from __future__ import annotations

import asyncio
import logging
import time

from demo_controller.kubernetes import KubernetesReconciler, render_resources
from demo_controller.store import DemoRecord, Store
from demo_controller.vault import VaultClient, VaultError

logger = logging.getLogger(__name__)


class ReconciliationLoop:
    def __init__(
        self,
        store: Store,
        vault: VaultClient,
        kubernetes: KubernetesReconciler,
        namespace: str,
        owner: str,
        image_registry: str,
        default_lifetime: int,
        interval: float,
    ):
        self.store = store
        self.vault = vault
        self.kubernetes = kubernetes
        self.namespace = namespace
        self.owner = owner
        self.image_registry = image_registry
        self.default_lifetime = default_lifetime
        self.interval = interval
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        while not self._stopping.is_set():
            for record in self.store.list_reconcilable():
                await self.reconcile(record)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping.set()

    async def reconcile(self, record: DemoRecord) -> None:
        if not record.desired_present or record.expires_at <= int(time.time()):
            self.store.update_state(record.repository, record.pr, "deleting", "deleting resources")
            try:
                await asyncio.to_thread(self.kubernetes.delete, record.repository, record.pr)
                self.store.delete_record(record.repository, record.pr)
            except Exception:
                logger.exception(
                    "Kubernetes deletion failed for %s#%d", record.repository, record.pr
                )
                self.store.update_state(
                    record.repository, record.pr, "failed", "Kubernetes deletion failed"
                )
            return
        self.store.update_state(
            record.repository, record.pr, "reconciling", "loading configuration"
        )
        try:
            project = await self.vault.project(record.repository)
            if project.config.image_namespace:
                expected = f"{self.image_registry}/{project.config.image_namespace.lower()}@sha256:"
                if not (record.image or "").lower().startswith(expected):
                    raise ValueError("image is outside the Vault-authorized namespace")
            rendered = render_resources(
                record, project.config, project.version, self.namespace, self.owner
            )
            ready = await asyncio.to_thread(self.kubernetes.apply, rendered)
            lifetime = min(
                project.config.lifetime_seconds or self.default_lifetime,
                self.default_lifetime,
            )
            self.store.update_state(
                record.repository,
                record.pr,
                "ready" if ready else "reconciling",
                "deployment is ready" if ready else "waiting for deployment readiness",
                vault_version=project.version,
                port=project.config.port,
                lifetime_seconds=lifetime if record.vault_version is None else None,
            )
        except VaultError as error:
            logger.warning("Vault reconciliation failed for %s#%d", record.repository, record.pr)
            message = (
                "update failed; previous deployment remains available"
                if record.port is not None
                else str(error)
            )
            self.store.update_state(record.repository, record.pr, "failed", message)
        except Exception as error:
            logger.exception("Reconciliation failed for %s#%d", record.repository, record.pr)
            detail = f"{type(error).__name__}: {str(error)[:300]}"
            self.store.update_state(
                record.repository,
                record.pr,
                "failed",
                f"Kubernetes reconciliation failed ({detail})",
            )
