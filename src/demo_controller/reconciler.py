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
        interval: float,
    ):
        self.store = store
        self.vault = vault
        self.kubernetes = kubernetes
        self.namespace = namespace
        self.owner = owner
        self.image_registry = image_registry
        self.interval = interval
        self._stopping = asyncio.Event()
        self.last_completed_at: int | None = None
        self._dependencies: dict[str, dict[str, str | int | None]] = {
            name: {"status": "unknown", "checked_at": None, "error": None}
            for name in ("vault", "kubernetes")
        }

    def health(self) -> dict[str, dict[str, str | int | None]]:
        # Cached observations only: a monitoring request must not log in to Vault,
        # apply Kubernetes resources, or disclose repository/secret data.
        return {name: dict(observation) for name, observation in self._dependencies.items()}

    def _observe(self, dependency: str, error: str | None = None) -> None:
        self._dependencies[dependency] = {
            "status": "error" if error else "ok",
            "checked_at": int(time.time()),
            "error": error,
        }

    async def run(self) -> None:
        try:
            while not self._stopping.is_set():
                for record in self.store.list_reconcilable():
                    await self.reconcile(record)
                self.last_completed_at = int(time.time())
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.interval)
                except TimeoutError:
                    pass
        finally:
            await self.vault.aclose()

    def stop(self) -> None:
        self._stopping.set()

    async def reconcile(self, record: DemoRecord) -> None:
        if not record.desired_present or record.expires_at <= int(time.time()):
            self.store.update_state(record.repository, record.pr, "deleting", "deleting resources")
            try:
                await asyncio.to_thread(self.kubernetes.delete, record.repository, record.pr)
                self._observe("kubernetes")
                self.store.delete_record(record.repository, record.pr)
            except Exception as error:
                self._observe("kubernetes", type(error).__name__)
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
            self._observe("vault")
            if project.config.image_namespace:
                expected = f"{self.image_registry}/{project.config.image_namespace.lower()}@sha256:"
                if not (record.image or "").lower().startswith(expected):
                    raise ValueError("image is outside the Vault-authorized namespace")
            rendered = render_resources(
                record, project.config, project.version, self.namespace, self.owner
            )
            try:
                ready = await asyncio.to_thread(self.kubernetes.apply, rendered)
            except Exception as error:
                self._observe("kubernetes", type(error).__name__)
                raise
            self._observe("kubernetes")
            self.store.update_state(
                record.repository,
                record.pr,
                "ready" if ready else "reconciling",
                "deployment is ready" if ready else "waiting for deployment readiness",
                vault_version=project.version,
                port=project.config.port,
            )
        except VaultError as error:
            self._observe("vault", error.category)
            logger.warning(
                "Vault reconciliation failed for %s#%d (%s)",
                record.repository,
                record.pr,
                error.category,
            )
            message = str(error)
            if record.port is not None:
                message += "; previous resources retained; backend availability is not verified"
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
