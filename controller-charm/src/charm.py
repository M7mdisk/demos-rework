#!/usr/bin/env python3
from __future__ import annotations

import json
import logging

import ops
from kubernetes import KubernetesAdapter, KubernetesPermissionError
from lightkube import ApiError, Client
from ops import pebble

logger = logging.getLogger(__name__)


class DemosControllerCharm(ops.CharmBase):
    _SERVICE = "fastapi"

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.container = self.unit.get_container("controller")
        self.kubernetes = KubernetesAdapter(
            Client(namespace=self.model.name), self.model.name, self.app.name
        )
        framework.observe(self.on.controller_pebble_ready, self._configure)
        framework.observe(self.on.config_changed, self._configure)
        framework.observe(self.on.secret_changed, self._configure)
        framework.observe(self.on.haproxy_route_relation_joined, self._publish_route)
        framework.observe(self.on.haproxy_route_relation_changed, self._publish_route)
        framework.observe(self.on.remove, self._remove)
        framework.observe(self.on.reconcile_action, self._reconcile_action)
        framework.observe(self.on.show_endpoint_action, self._show_endpoint_action)
        framework.observe(self.on.update_status, self._update_status)

    def _secret_content(self, config_name: str) -> dict[str, str] | None:
        secret_id = str(self.config.get(config_name) or "")
        if not secret_id:
            return None
        try:
            return self.model.get_secret(id=secret_id).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError):
            logger.warning("Configured %s is unavailable", config_name)
            return None

    def _environment(self) -> dict[str, str] | None:
        approle = self._secret_content("vault-approle-secret")
        hmac_secret = self._secret_content("hmac-credentials-secret")
        if not approle or not {"role-id", "secret-id"} <= approle.keys():
            return None
        if not hmac_secret or "credentials" not in hmac_secret:
            return None
        try:
            credentials = json.loads(hmac_secret["credentials"])
            if not isinstance(credentials, dict) or not credentials:
                return None
        except (TypeError, json.JSONDecodeError):
            return None
        return {
            "DATABASE_PATH": "/data/controller.db",
            "KUBERNETES_NAMESPACE": self.model.name,
            "CONTROLLER_OWNER": self.app.name,
            "HOSTNAME_SUFFIX": str(self.config["hostname-suffix"]).strip("."),
            "IMAGE_REGISTRY": str(self.config["image-registry"])
            .strip()
            .strip("/")
            .lower(),
            "UVICORN_HOST": "0.0.0.0",
            "UVICORN_PORT": "8080",
            "VAULT_ADDRESS": str(self.config["vault-address"]),
            "VAULT_NAMESPACE": str(self.config["vault-namespace"]),
            "VAULT_KV_MOUNT": str(self.config["vault-kv-mount"]),
            "VAULT_BASE_PATH": str(self.config["vault-base-path"]),
            "VAULT_ROLE_ID": approle["role-id"],
            "VAULT_SECRET_ID": approle["secret-id"],
            "HMAC_CREDENTIALS": json.dumps(
                credentials, separators=(",", ":"), sort_keys=True
            ),
            "MAX_DEMO_LIFETIME": str(self.config["max-demo-lifetime"]),
            "RECONCILE_INTERVAL": str(self.config["reconcile-interval"]),
            "REQUEST_MAX_BYTES": str(self.config["request-max-bytes"]),
            "SIGNATURE_MAX_AGE": str(self.config["signature-max-age"]),
            "RATE_LIMIT_PER_MINUTE": str(self.config["rate-limit-per-minute"]),
        }

    def _configure(self, _: ops.EventBase) -> None:
        environment = self._environment()
        if not environment:
            self.unit.status = ops.BlockedStatus(
                "configure Vault AppRole and HMAC secrets"
            )
            return
        if not self.container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for controller container")
            return
        layer = pebble.Layer(
            {
                "summary": "demos controller",
                "description": "deployment API, reconciler, and host proxy",
                "services": {
                    self._SERVICE: {
                        "override": "replace",
                        "summary": "demos controller",
                        "command": "/bin/python3 -m uvicorn app:app",
                        "startup": "enabled",
                        "working-dir": "/app",
                        "environment": environment,
                    }
                },
                "checks": {
                    "ready": {
                        "override": "replace",
                        "level": "ready",
                        "http": {"url": "http://localhost:8080/readyz"},
                        "period": "10s",
                        "timeout": "3s",
                        "threshold": 3,
                    }
                },
            }
        )
        self.container.add_layer("controller", layer, combine=True)
        self.unit.open_port("tcp", 8080)
        self.container.replan()
        if not self.model.relations["haproxy-route"]:
            self.unit.status = ops.WaitingStatus("waiting for haproxy-route relation")
            return
        if self._publish_route(None):
            self.unit.status = ops.ActiveStatus()

    def _publish_route(self, _: ops.EventBase | None) -> bool:
        if not self.unit.is_leader():
            return False
        try:
            endpoint = self.kubernetes.reconcile()
        except KubernetesPermissionError:
            self.unit.status = ops.BlockedStatus(
                "Kubernetes API access denied; deploy this charm with --trust"
            )
            return False
        except (ApiError, RuntimeError):
            logger.exception("Unable to configure the controller NodePort")
            self.unit.status = ops.WaitingStatus("waiting for controller NodePort")
            return False
        suffix = str(self.config["hostname-suffix"]).strip(".")
        data = {
            "service": endpoint.service_name,
            "ports": [endpoint.port],
            "protocol": "http",
            "hosts": endpoint.hosts,
            "hostname": f"*.{suffix}",
            "paths": ["/"],
            "check": {
                "interval": 10,
                "rise": 2,
                "fall": 3,
                "path": "/readyz",
                "port": endpoint.port,
            },
        }
        for relation in self.model.relations["haproxy-route"]:
            relation.data[self.app].clear()
            relation.data[self.app].update(
                {
                    key: json.dumps(value, separators=(",", ":"))
                    for key, value in data.items()
                }
            )
        return True

    def _remove(self, _: ops.RemoveEvent) -> None:
        if not self.unit.is_leader():
            return
        try:
            self.kubernetes.cleanup()
        except (ApiError, KubernetesPermissionError):
            logger.exception("Unable to remove the owned controller NodePort")

    def _reconcile_action(self, event: ops.ActionEvent) -> None:
        if not self.container.can_connect():
            event.fail("controller container is unavailable")
            return
        self.container.restart(self._SERVICE)
        event.set_results(
            {"result": "controller restarted; reconciliation will resume"}
        )

    def _show_endpoint_action(self, event: ops.ActionEvent) -> None:
        suffix = str(self.config["hostname-suffix"]).strip(".")
        event.set_results(
            {
                "hostname": f"*.{suffix}",
                "service": f"{self.app.name}.{self.model.name}.svc.cluster.local:8080",
            }
        )

    def _update_status(self, _: ops.UpdateStatusEvent) -> None:
        environment = self._environment()
        if not environment:
            self.unit.status = ops.BlockedStatus(
                "configure Vault AppRole and HMAC secrets"
            )
        elif not self.container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for controller container")
        else:
            services = self.container.get_services(self._SERVICE)
            if not services or not services[self._SERVICE].is_running():
                self.unit.status = ops.MaintenanceStatus(
                    "controller service is not running"
                )
            elif not self.model.relations["haproxy-route"]:
                self.unit.status = ops.WaitingStatus(
                    "waiting for haproxy-route relation"
                )
            elif self.unit.is_leader() and not self._publish_route(None):
                return
            else:
                self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":
    ops.main(DemosControllerCharm)
