from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from demo_controller.api import create_app
from demo_controller.auth import Authenticator
from demo_controller.models import DeployRequest
from demo_controller.settings import Settings
from demo_controller.store import Store


def signed_request(
    path: str, body: bytes, nonce: str, key: str, timestamp: str | None = None
) -> Request:
    timestamp = timestamp or str(int(time.time()))
    canonical = b"\n".join((b"POST", path.encode(), timestamp.encode(), nonce.encode(), body))
    signature = hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()
    headers = [
        (b"x-demos-repository", b"canonical/example"),
        (b"x-demos-timestamp", timestamp.encode()),
        (b"x-demos-nonce", nonce.encode()),
        (b"x-demos-signature", f"sha256={signature}".encode()),
    ]
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b""},
        receive,
    )


async def test_hmac_authentication_and_replay_protection(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", "demos.canonical.com")
    auth = Authenticator({"canonical/example": "key"}, store, 300, 60)
    body = b'{"repository":"canonical/example"}'
    timestamp = str(int(time.time()))
    request = signed_request("/api/v1/deploy", body, "delivery-123", "key", timestamp)
    assert await auth.authenticate(request) == "canonical/example"
    idempotent_retry = signed_request(
        "/api/v1/deploy", body, "delivery-123", "key", str(int(timestamp) + 1)
    )
    assert await auth.authenticate(idempotent_retry) == "canonical/example"
    replay = signed_request(
        "/api/v1/deploy",
        b'{"repository":"canonical/other"}',
        "delivery-123",
        "key",
        timestamp,
    )
    with pytest.raises(HTTPException) as error:
        await auth.authenticate(replay)
    assert error.value.status_code == 409


async def test_invalid_signature_does_not_consume_repository_rate_limit(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db", "demos.canonical.com")
    auth = Authenticator({"canonical/example": "key"}, store, 300, 1)
    body = b'{"repository":"canonical/example"}'
    invalid = signed_request("/api/v1/deploy", body, "invalid-request", "wrong-key")
    with pytest.raises(HTTPException) as error:
        await auth.authenticate(invalid)
    assert error.value.status_code == 401

    valid = signed_request("/api/v1/deploy", body, "valid-request", "key")
    assert await auth.authenticate(valid) == "canonical/example"


def test_store_is_idempotent_and_contains_no_project_secrets(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = Store(path, "demos.canonical.com")
    request = DeployRequest(
        repository="canonical/example",
        pr=8,
        commit_sha="a" * 40,
        image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-8",
    )
    first, created = store.upsert_deploy(request, "same-hash", 3600)
    second, changed = store.upsert_deploy(request, "same-hash", 3600)
    assert created is True
    assert changed is False
    assert first == second
    assert b"application-secret-value" not in path.read_bytes()


def test_deploy_update_preserves_the_active_route_until_reconciled(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", "demos.canonical.com")
    first = DeployRequest(
        repository="canonical/example",
        pr=8,
        commit_sha="a" * 40,
        image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-8-a",
    )
    store.upsert_deploy(first, "first-hash", 3600)
    store.update_state(
        first.repository,
        first.pr,
        "ready",
        "deployment is ready",
        vault_version=2,
        port=8000,
    )
    update = first.model_copy(
        update={
            "commit_sha": "c" * 40,
            "image": f"ghcr.io/canonical/example@sha256:{'d' * 64}",
            "delivery_id": "delivery-8-b",
        }
    )

    pending, changed = store.upsert_deploy(update, "second-hash", 3600)

    assert changed is True
    assert pending.state == "pending"
    assert pending.port == 8000
    assert pending.vault_version == 2
    assert store.get_ready_by_hostname(store.status(pending).hostname) == pending


def test_deploy_api_accepts_a_signed_request(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", "demos.canonical.com")
    auth = Authenticator({"canonical/example": "key"}, store, 300, 60)

    class ReconciliationStub:
        async def run(self) -> None:
            return

        def stop(self) -> None:
            return

    settings = Settings(
        database_path=tmp_path / "state.db",
        namespace="testing",
        hostname_suffix="demos.canonical.com",
        controller_owner="controller",
        image_registry="ghcr.io",
        vault_address="https://vault.example",
        vault_namespace=None,
        vault_kv_mount="kv",
        vault_base_path="demos",
        vault_role_id="role",
        vault_secret_id="secret",
        hmac_credentials={"canonical/example": "key"},
        max_demo_lifetime_seconds=3600,
        reconcile_interval_seconds=10,
        request_max_bytes=65536,
        signature_max_age_seconds=300,
        rate_limit_per_minute=60,
        proxy_timeout_seconds=3,
    )
    app = create_app(settings, store, auth, ReconciliationStub())  # type: ignore[arg-type]
    body = (
        '{"repository":"canonical/example","pr":12,"commit_sha":"'
        + "a" * 40
        + '","image":"ghcr.io/canonical/example@sha256:'
        + "b" * 64
        + '","delivery_id":"delivery-12"}'
    ).encode()
    timestamp = str(int(time.time()))
    canonical = b"\n".join((b"POST", b"/api/v1/deploy", timestamp.encode(), b"delivery-12", body))
    signature = hmac.new(b"key", canonical, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/deploy",
            content=body,
            headers={
                "content-type": "application/json",
                "x-demos-repository": "canonical/example",
                "x-demos-timestamp": timestamp,
                "x-demos-nonce": "delivery-12",
                "x-demos-signature": f"sha256={signature}",
            },
        )
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
