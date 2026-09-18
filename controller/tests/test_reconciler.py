from __future__ import annotations

from pathlib import Path

from demo_controller.models import DeployRequest, ProjectConfig
from demo_controller.naming import hostname
from demo_controller.reconciler import ReconciliationLoop
from demo_controller.store import Store
from demo_controller.vault import VaultError, VaultProject


class UnavailableVault:
    async def project(self, repository: str):
        raise VaultError("Vault project lookup failed")


class KubernetesStub:
    def apply(self, resources):
        raise AssertionError("Kubernetes apply must not run when Vault is unavailable")

    def delete(self, repository: str, pr: int) -> None:
        raise AssertionError("Kubernetes delete must not run for an active demo")


class AvailableVault:
    async def project(self, repository: str):
        return VaultProject(
            config=ProjectConfig(
                schema_version=1,
                enabled=True,
                image_namespace=repository,
            ),
            version=1,
        )


class ApplyingKubernetes:
    def __init__(self):
        self.applied = False

    def apply(self, resources):
        self.applied = True
        return True

    def delete(self, repository: str, pr: int) -> None:
        raise AssertionError("Kubernetes delete must not run for an active demo")


async def test_vault_outage_keeps_existing_ready_demo_routable(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", "demos.canonical.com")
    request = DeployRequest(
        repository="canonical/example",
        pr=8,
        commit_sha="a" * 40,
        image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-8",
    )
    store.upsert_deploy(request, "request-hash", 3600)
    store.update_state(
        request.repository,
        request.pr,
        "ready",
        "deployment is ready",
        vault_version=2,
        port=8000,
    )
    record = store.get(request.repository, request.pr)
    assert record is not None
    loop = ReconciliationLoop(
        store,
        UnavailableVault(),  # type: ignore[arg-type]
        KubernetesStub(),  # type: ignore[arg-type]
        "testing",
        "controller",
        "ghcr.io",
        3600,
        10,
    )

    await loop.reconcile(record)

    updated = store.get(request.repository, request.pr)
    assert updated is not None
    assert updated.state == "failed"
    assert updated.message == "update failed; previous deployment remains available"
    assert (
        store.get_ready_by_hostname(hostname(updated.repository, updated.pr, "demos.canonical.com"))
        == updated
    )


async def test_vault_image_namespace_accepts_repository_digest(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", "demos.canonical.com")
    request = DeployRequest(
        repository="canonical/example",
        pr=9,
        commit_sha="a" * 40,
        image=f"localhost:32000/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-9",
    )
    store.upsert_deploy(request, "request-hash", 3600)
    record = store.get(request.repository, request.pr)
    assert record is not None
    kubernetes = ApplyingKubernetes()
    loop = ReconciliationLoop(
        store,
        AvailableVault(),  # type: ignore[arg-type]
        kubernetes,  # type: ignore[arg-type]
        "testing",
        "controller",
        "localhost:32000",
        3600,
        10,
    )

    await loop.reconcile(record)

    updated = store.get(request.repository, request.pr)
    assert updated is not None
    assert updated.state == "ready"
    assert kubernetes.applied is True
