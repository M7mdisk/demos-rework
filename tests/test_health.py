from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_controller.api import create_app
from demo_controller.auth import Authenticator
from demo_controller.models import DeployRequest, ProjectConfig
from demo_controller.reconciler import ReconciliationLoop
from demo_controller.settings import Settings
from demo_controller.store import Store
from demo_controller.vault import VaultError, VaultProject


@pytest.fixture
def controller(tmp_path: Path):
    store = Store(tmp_path / "state.db", "demos.example")
    vault = MagicMock()
    vault.project = AsyncMock(
        return_value=VaultProject(ProjectConfig(schema_version=1, enabled=True), 1)
    )
    vault.aclose = AsyncMock()
    kubernetes = MagicMock()
    kubernetes.apply.return_value = True
    loop = ReconciliationLoop(store, vault, kubernetes, "testing", "controller", "ghcr.io", 0.01)
    settings = Settings(
        database_path=tmp_path / "state.db",
        namespace="testing",
        hostname_suffix="demos.example",
        controller_owner="controller",
        image_registry="ghcr.io",
        vault_address="https://vault.example",
        vault_namespace=None,
        vault_kv_mount="kv",
        vault_base_path="demos",
        vault_role_id="test-role",
        vault_secret_id="test-secret",
        hmac_credentials={"canonical/example": "test-key"},
        reconcile_interval_seconds=0.01,
        request_max_bytes=65536,
        signature_max_age_seconds=300,
        rate_limit_per_minute=60,
        proxy_timeout_seconds=3,
    )
    auth = Authenticator(settings.hmac_credentials, store, 300, 60)
    app = create_app(settings, store, auth, loop)
    return app, store, loop, vault, kubernetes


def add_demo(store: Store):
    store.upsert_deploy(
        DeployRequest(
            repository="canonical/example",
            pr=1,
            commit_sha="a" * 40,
            image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
            delivery_id="delivery-1",
        ),
        "test-hash",
    )


def wait_for_status(client: TestClient, predicate):
    for _ in range(200):
        response = client.get("/statusz")
        if predicate(response.json()):
            return response
        time.sleep(0.005)
    raise AssertionError("controller status did not reach expected state")


def test_idle_health_reports_unchecked_dependencies_without_probing(controller) -> None:
    app, _, _, vault, kubernetes = controller
    with TestClient(app) as client:
        response = client.get("/statusz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["reconciliation"]["state"] == "running"
        assert all(
            item == {"status": "unknown", "checked_at": None, "error": None}
            for item in data["dependencies"].values()
        )
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/healthz").json() == {"status": "ok"}
        vault.project.assert_not_called()
        kubernetes.apply.assert_not_called()
    vault.aclose.assert_awaited_once()


def test_vault_failure_degrades_status_not_ingress_and_recovers(controller) -> None:
    app, store, _, vault, _ = controller
    add_demo(store)
    vault.project.side_effect = VaultError(
        "Vault authentication failed (PoolTimeout)", category="authentication_PoolTimeout"
    )
    with TestClient(app) as client:
        response = wait_for_status(client, lambda d: d["demos"]["failed"] == 1)
        assert response.status_code == 503
        assert response.json()["dependencies"]["vault"]["error"] == "authentication_PoolTimeout"
        assert response.json()["dependencies"]["vault"]["checked_at"] is not None
        assert client.get("/readyz").status_code == 200
        assert "canonical/example" not in response.text
        assert "test-secret" not in response.text
        vault.project.side_effect = None
        response = wait_for_status(client, lambda d: d["demos"]["ready"] == 1)
        assert response.status_code == 200
        assert all(d["status"] == "ok" for d in response.json()["dependencies"].values())


def test_kubernetes_error_is_reported_separately(controller) -> None:
    app, store, _, _, kubernetes = controller
    add_demo(store)
    kubernetes.apply.side_effect = RuntimeError("test backend failure")
    with TestClient(app) as client:
        response = wait_for_status(client, lambda d: d["demos"]["failed"] == 1)
        data = response.json()
        assert response.status_code == 503
        assert data["dependencies"]["vault"]["status"] == "ok"
        assert data["dependencies"]["kubernetes"]["error"] == "RuntimeError"
        assert "test backend failure" not in response.text
        assert client.get("/readyz").status_code == 200


def test_stopped_reconciler_is_visible_without_exposing_exception(controller, caplog) -> None:
    app, _, loop, _, _ = controller

    async def failed():
        raise RuntimeError("sensitive failure detail")

    loop.run = failed
    with TestClient(app) as client:
        response = wait_for_status(client, lambda d: d["reconciliation"]["state"] == "stopped")
        assert response.status_code == 503
        assert client.get("/readyz").status_code == 200
        assert "sensitive failure detail" not in response.text + caplog.text
    assert "Reconciliation loop stopped (RuntimeError)" in caplog.text


def test_database_failure_returns_sanitized_503(controller, monkeypatch) -> None:
    app, store, _, _, _ = controller
    with TestClient(app) as client:

        def failed():
            raise sqlite3.OperationalError("sensitive filesystem path")

        monkeypatch.setattr(store, "list_reconcilable", failed)
        response = client.get("/statusz")
        assert response.status_code == 503
        assert response.json()["database"] == "error"
        assert "sensitive filesystem path" not in response.text
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"detail": "state database unavailable"}


async def test_cancelling_reconciliation_closes_vault_client(controller) -> None:
    _, _, loop, vault, _ = controller
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    vault.aclose.assert_awaited_once()


async def test_proxy_client_owned_by_caller_is_not_closed(controller) -> None:
    _, store, loop, _, _ = controller
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as proxy:
        auth = Authenticator({}, store, 300, 60)
        application = create_app(MagicMock(spec=Settings), store, auth, loop, proxy_client=proxy)
        with TestClient(application) as client:
            assert client.get("/readyz").status_code == 200
        assert not proxy.is_closed


@pytest.mark.parametrize(
    "repository, timestamp_offset, expected_status",
    [("example", 0, 200), ("other", 0, 403), ("example", -301, 401), ("example", 301, 401)],
)
def test_authenticated_status_preserves_signature_and_repository_isolation(
    controller, repository, timestamp_offset, expected_status
) -> None:
    app, store, _, _, _ = controller
    add_demo(store)
    path = f"/api/v1/demos/canonical/{repository}/1"
    timestamp = str(int(time.time()) + timestamp_offset)
    nonce = "status-request"
    canonical = b"\n".join((b"GET", path.encode(), timestamp.encode(), nonce.encode(), b""))
    signature = hmac.new(b"test-key", canonical, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        response = client.get(
            path,
            headers={
                "x-demos-repository": "canonical/example",
                "x-demos-timestamp": timestamp,
                "x-demos-nonce": nonce,
                "x-demos-signature": f"sha256={signature}",
            },
        )
        assert response.status_code == expected_status
