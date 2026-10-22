from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from demo_controller.models import DeployRequest, ProjectConfig
from demo_controller.reconciler import ReconciliationLoop
from demo_controller.routing import CaddyRoutes
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
    store.upsert_deploy(request, "request-hash")
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
        10,
    )

    await loop.reconcile(record)

    updated = store.get(request.repository, request.pr)
    assert updated is not None
    assert updated.state == "failed"
    assert updated.message == (
        "Vault project lookup failed; previous resources retained; "
        "backend availability is not verified"
    )
    assert loop.health()["vault"]["status"] == "error"
    assert store.list_routable() == [updated]


async def test_vault_image_namespace_accepts_repository_digest(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", "demos.canonical.com")
    request = DeployRequest(
        repository="canonical/example",
        pr=9,
        commit_sha="a" * 40,
        image=f"localhost:32000/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-9",
    )
    store.upsert_deploy(request, "request-hash")
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
        10,
    )

    await loop.reconcile(record)

    updated = store.get(request.repository, request.pr)
    assert updated is not None
    assert updated.state == "ready"
    assert kubernetes.applied is True
    assert loop.health()["vault"]["status"] == "ok"
    assert loop.health()["kubernetes"]["status"] == "ok"


@pytest.fixture
def routed_controller(tmp_path):
    store = Store(tmp_path / "state.db", "demos.example")
    store.upsert_deploy(
        DeployRequest(
            repository="canonical/example",
            pr=1,
            commit_sha="a" * 40,
            image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
            delivery_id="delivery-1",
        ),
        "hash",
    )
    vault = MagicMock()
    vault.project = AsyncMock(
        return_value=VaultProject(ProjectConfig(schema_version=1, enabled=True, port=9000), 1)
    )
    vault.aclose = AsyncMock()
    kubernetes = MagicMock()
    kubernetes.apply.return_value = True
    routes = MagicMock(spec=CaddyRoutes)
    routes.sync = AsyncMock()
    routes.aclose = AsyncMock()
    loop = ReconciliationLoop(store, vault, kubernetes, "testing", "owner", "ghcr.io", 0.01, routes)
    return store, vault, kubernetes, routes, loop


async def test_new_demo_is_not_routable_until_ready(routed_controller):
    store, _, kubernetes, routes, loop = routed_controller
    kubernetes.apply.return_value = False
    await loop.reconcile(store.get("canonical/example", 1))
    assert store.list_routable() == []
    assert store.get("canonical/example", 1).port is None
    routes.sync.assert_not_called()

    kubernetes.apply.return_value = True
    await loop.reconcile(store.get("canonical/example", 1))
    assert store.get("canonical/example", 1).state == "ready"
    assert store.list_routable()[0].port == 9000
    assert routes.sync.await_args.args[0][0].port == 9000


async def test_unready_replacement_keeps_last_ready_port(routed_controller):
    store, _, kubernetes, routes, loop = routed_controller
    store.update_state("canonical/example", 1, "ready", "", port=8000)
    kubernetes.apply.return_value = False
    await loop.reconcile(store.get("canonical/example", 1))
    assert store.list_routable()[0].port == 8000
    routes.sync.assert_not_called()


async def test_route_failure_does_not_publish_ready_or_lose_previous_port(routed_controller):
    store, _, kubernetes, routes, loop = routed_controller
    store.update_state("canonical/example", 1, "ready", "", port=8000)
    routes.sync.side_effect = RuntimeError("synthetic admin failure")
    await loop.reconcile(store.get("canonical/example", 1))
    updated = store.get("canonical/example", 1)
    assert updated.state == "failed"
    assert updated.port == 8000
    assert updated.message == "Caddy route publication failed"
    assert loop.health()["caddy"]["status"] == "error"
    kubernetes.delete.assert_not_called()

    routes.sync.side_effect = None
    await loop.reconcile(updated)
    assert store.get("canonical/example", 1).state == "ready"
    assert store.list_routable()[0].port == 9000
    assert loop.health()["caddy"]["status"] == "ok"


async def test_route_withdrawal_must_succeed_before_backend_deletion(routed_controller):
    store, _, kubernetes, routes, loop = routed_controller
    store.update_state("canonical/example", 1, "ready", "", port=8000)
    store.request_destroy("canonical/example", 1, "destroy-1", "destroy-hash")
    routes.sync.side_effect = RuntimeError("synthetic admin failure")
    await loop.reconcile(store.get("canonical/example", 1))
    assert store.get("canonical/example", 1).state == "deleting"
    kubernetes.delete.assert_not_called()

    routes.sync.side_effect = None

    def assert_route_withdrawn(repository, pr):
        assert routes.sync.await_args.args[0] == []

    kubernetes.delete.side_effect = assert_route_withdrawn
    await loop.reconcile(store.get("canonical/example", 1))
    kubernetes.delete.assert_called_once_with("canonical/example", 1)
    assert store.get("canonical/example", 1) is None


async def test_retained_routes_restore_before_vault_and_clients_close(routed_controller):
    store, vault, _, routes, loop = routed_controller
    store.update_state("canonical/example", 1, "ready", "", port=8000)

    async def unavailable(repository):
        assert routes.sync.await_args.args[0][0].port == 8000
        loop.stop()
        raise VaultError("Vault project lookup failed")

    vault.project.side_effect = unavailable
    await asyncio.wait_for(loop.run(), timeout=5)
    assert store.list_routable()[0].port == 8000
    assert loop.health()["vault"]["status"] == "error"
    vault.aclose.assert_awaited_once()
    routes.aclose.assert_awaited_once()
