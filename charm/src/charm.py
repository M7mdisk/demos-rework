#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re

import ops
from demo_logs import (
    DEFAULT_TAIL,
    MAX_TAIL,
    DemoLogsError,
    fetch_demo_logs,
)
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
        framework.observe(self.on.demo_logs_action, self._demo_logs_action)
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
        environment = {
            "DATABASE_PATH": "/data/controller.db",
            "KUBERNETES_NAMESPACE": self.model.name,
            "CONTROLLER_OWNER": self.app.name,
            "HOSTNAME_SUFFIX": str(self.config["hostname-suffix"]).strip("."),
            "IMAGE_REGISTRY": str(self.config["image-registry"]).strip().strip("/").lower(),
            "API_HOSTNAME": str(self.config["api-hostname"]).strip("."),
            "UVICORN_HOST": "127.0.0.1",
            "UVICORN_PORT": "8081",
            "VAULT_ADDRESS": str(self.config["vault-address"]),
            "VAULT_NAMESPACE": str(self.config["vault-namespace"]),
            "VAULT_KV_MOUNT": str(self.config["vault-kv-mount"]),
            "VAULT_BASE_PATH": str(self.config["vault-base-path"]),
            "VAULT_ROLE_ID": approle["role-id"],
            "VAULT_SECRET_ID": approle["secret-id"],
            "HMAC_CREDENTIALS": json.dumps(credentials, separators=(",", ":"), sort_keys=True),
            "RECONCILE_INTERVAL": str(self.config["reconcile-interval"]),
            "REQUEST_MAX_BYTES": str(self.config["request-max-bytes"]),
            "SIGNATURE_MAX_AGE": str(self.config["signature-max-age"]),
            "RATE_LIMIT_PER_MINUTE": str(self.config["rate-limit-per-minute"]),
        }
        for juju_name, standard_name in (
            ("JUJU_CHARM_HTTP_PROXY", "HTTP_PROXY"),
            ("JUJU_CHARM_HTTPS_PROXY", "HTTPS_PROXY"),
        ):
            if value := os.getenv(juju_name):
                environment[standard_name] = value
        no_proxy = ["localhost", "127.0.0.1"]
        no_proxy.extend(item for item in os.getenv("JUJU_CHARM_NO_PROXY", "").split(",") if item)
        if kubernetes_service_host := os.getenv("KUBERNETES_SERVICE_HOST"):
            no_proxy.append(kubernetes_service_host)
        if no_proxy:
            environment["NO_PROXY"] = ",".join(dict.fromkeys(no_proxy))
        return environment

    def _configure(self, _: ops.EventBase) -> None:
        environment = self._environment()
        if not environment:
            self.unit.status = ops.BlockedStatus("configure Vault AppRole and HMAC secrets")
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
                    },
                    "caddy": {
                        "override": "replace",
                        "summary": "stateless demo reverse proxy",
                        "command": "/bin/python3 -m demo_controller.routing",
                        "startup": "enabled",
                        "working-dir": "/app",
                        # No Vault/HMAC credentials or egress proxy in Caddy.
                        "environment": {
                            key: environment[key]
                            for key in ("API_HOSTNAME", "KUBERNETES_NAMESPACE", "HOSTNAME_SUFFIX")
                        }
                        | {"XDG_CONFIG_HOME": "/data", "XDG_DATA_HOME": "/data"},
                    },
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
            self._set_workload_status()

    def _set_workload_status(self) -> None:
        # This is deliberately separate from the ingress /readyz check: a Vault
        # failure must be visible in Juju without withdrawing retained demo routes.
        command = [
            "/bin/python3",
            "-c",
            (
                "import urllib.error, urllib.request\n"
                "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
                "try:\n"
                "    response = opener.open('http://localhost:8081/statusz', timeout=3)\n"
                "except urllib.error.HTTPError as error:\n"
                "    response = error\n"
                "with response:\n"
                "    print(response.read(16384).decode())\n"
            ),
        ]
        try:
            output, _ = self.container.exec(command, timeout=5).wait_output()
            health = json.loads(output)
            if health["database"] != "ok":
                self.unit.status = ops.WaitingStatus("controller state database unavailable")
            elif health["reconciliation"]["state"] != "running":
                self.unit.status = ops.MaintenanceStatus("controller reconciliation loop stopped")
            elif health["dependencies"]["vault"]["status"] == "error":
                self.unit.status = ops.WaitingStatus(
                    "Vault reconciliation degraded; see controller logs"
                )
            elif health["dependencies"]["kubernetes"]["status"] == "error":
                self.unit.status = ops.WaitingStatus(
                    "Kubernetes reconciliation degraded; see controller logs"
                )
            elif health["dependencies"].get("caddy", {}).get("status") == "error":
                self.unit.status = ops.WaitingStatus(
                    "Caddy route synchronization degraded; retained routes still served"
                )
            elif health["status"] == "degraded":
                self.unit.status = ops.WaitingStatus(
                    "demo reconciliation degraded; see controller logs"
                )
            elif health["status"] == "ok":
                self.unit.status = ops.ActiveStatus()
            else:
                self.unit.status = ops.WaitingStatus("waiting for controller diagnostics")
        except (pebble.Error, OSError, ValueError, KeyError, TypeError, TimeoutError):
            self.unit.status = ops.WaitingStatus("waiting for controller diagnostics")

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
            "hostname": str(self.config["api-hostname"]).strip("."),
            "additional_hostnames": [f"*.{suffix}"],
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
                {key: json.dumps(value, separators=(",", ":")) for key, value in data.items()}
            )
            relation.data[self.unit]["address"] = json.dumps(
                str(self.model.get_binding(relation).network.bind_address)
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
        event.set_results({"result": "controller restarted; reconciliation will resume"})

    def _show_endpoint_action(self, event: ops.ActionEvent) -> None:
        suffix = str(self.config["hostname-suffix"]).strip(".")
        event.set_results(
            {
                "api-url": f"https://{str(self.config['api-hostname']).strip('.')}",
                "demo-hostname": f"*.{suffix}",
                "service": f"{self.app.name}.{self.model.name}.svc.cluster.local:8080",
            }
        )

    def _demo_logs_action(self, event: ops.ActionEvent) -> None:
        repository = str(event.params["repository"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            event.fail("repository must be the owner/name GitHub repository")
            return
        tail = min(max(int(event.params.get("tail") or DEFAULT_TAIL), 1), MAX_TAIL)
        try:
            logs = fetch_demo_logs(
                self.kubernetes.client, repository, int(event.params["pr"]), tail
            )
        except DemoLogsError as error:
            event.fail(str(error))
            return
        event.set_results({"logs": logs, "tail": tail})

    def _update_status(self, _: ops.UpdateStatusEvent) -> None:
        environment = self._environment()
        if not environment:
            self.unit.status = ops.BlockedStatus("configure Vault AppRole and HMAC secrets")
        elif not self.container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for controller container")
        else:
            services = self.container.get_services(self._SERVICE, "caddy")
            if "caddy" not in services or not services["caddy"].is_running():
                self.unit.status = ops.MaintenanceStatus("Caddy service is not running")
            elif self._SERVICE not in services or not services[self._SERVICE].is_running():
                self.unit.status = ops.MaintenanceStatus("controller service is not running")
            elif not self.model.relations["haproxy-route"]:
                self.unit.status = ops.WaitingStatus("waiting for haproxy-route relation")
            elif self.unit.is_leader() and not self._publish_route(None):
                return
            else:
                self._set_workload_status()


if __name__ == "__main__":
    ops.main(DemosControllerCharm)
