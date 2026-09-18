from __future__ import annotations

import json

import httpx

from demo_controller.vault import VaultClient


async def test_approle_login_and_kv_v2_project_lookup() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/approle/login"):
            return httpx.Response(
                200,
                json={
                    "auth": {
                        "client_token": "sensitive-token",
                        "lease_duration": 3600,
                        "renewable": True,
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": {
                        "schema_version": 1,
                        "enabled": True,
                        "environment": {"PUBLIC": "value"},
                        "secrets": {"PRIVATE": "secret"},
                    },
                    "metadata": {"version": 9},
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    vault = VaultClient(
        "https://vault.example",
        "role",
        "secret-id",
        "kv",
        "demos",
        namespace="admin",
        client=http,
    )
    project = await vault.project("canonical/example")
    assert project.version == 9
    assert project.config.secrets["PRIVATE"] == "secret"
    assert requests[1].url.path == "/v1/kv/data/demos/v1/canonical/example"
    assert requests[1].headers["x-vault-token"] == "sensitive-token"
    assert json.loads(requests[0].content) == {"role_id": "role", "secret_id": "secret-id"}
    await http.aclose()


async def test_project_lookup_reauthenticates_once_after_revoked_token() -> None:
    login_count = 0
    project_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count, project_count
        if request.url.path.endswith("/auth/approle/login"):
            login_count += 1
            return httpx.Response(
                200,
                json={
                    "auth": {
                        "client_token": f"token-{login_count}",
                        "lease_duration": 3600,
                        "renewable": False,
                    }
                },
            )
        project_count += 1
        if project_count == 1:
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": {"schema_version": 1, "enabled": True},
                    "metadata": {"version": 1},
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    vault = VaultClient(
        "https://vault.example",
        "role",
        "secret-id",
        "kv",
        "demos",
        client=http,
    )

    project = await vault.project("canonical/example")

    assert project.version == 1
    assert login_count == 2
    assert project_count == 2
    await http.aclose()
