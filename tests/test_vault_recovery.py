from __future__ import annotations

import asyncio
import json
import ssl

import httpcore
import httpx
import pytest

from demo_controller.vault import VaultClient, VaultError


def successful_response(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith(("/login", "/renew-self")):
        return httpx.Response(
            200,
            json={
                "auth": {"client_token": "test-token", "lease_duration": 3600, "renewable": True}
            },
        )
    return httpx.Response(
        200,
        json={
            "data": {
                "data": {"schema_version": 1, "enabled": True},
                "metadata": {"version": 3},
            }
        },
    )


@pytest.mark.parametrize(
    "error_type", [httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.ProxyError]
)
async def test_pre_send_failure_replaces_pool_and_retries_once(error_type, caplog) -> None:
    clients = []
    requests = []

    def failed(request):
        requests.append(request)
        raise error_type("credential-bearing transport error must not be logged")

    def recovered(request):
        requests.append(request)
        return successful_response(request)

    def factory():
        handler = failed if not clients else recovered
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    vault = VaultClient(
        "https://vault.example",
        "role",
        "test-secret",
        "kv",
        "demos",
        namespace="team",
        client_factory=factory,
    )
    try:
        project = await vault.project("canonical/example")
        assert project.version == 3
        assert len(clients) == 2
        assert clients[0].is_closed
        assert not clients[1].is_closed
        assert len(requests) == 3  # Failed login, successful login, project GET.
        assert json.loads(requests[0].content) == json.loads(requests[1].content)
        assert all(r.headers["x-vault-namespace"] == "team" for r in requests)
        assert "x-vault-token" not in requests[1].headers
        assert requests[2].headers["x-vault-token"] == "test-token"
        assert "credential-bearing" not in caplog.text
    finally:
        await vault.aclose()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ReadError, httpx.WriteError])
async def test_ambiguous_login_failure_is_not_replayed(error_type, caplog) -> None:
    clients = []
    attempts = []

    def failed(request):
        attempts.append(request)
        raise error_type("secret response details")

    def factory():
        handler = failed if not clients else successful_response
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    vault = VaultClient(
        "https://vault.example", "role", "test-secret", "kv", "demos", client_factory=factory
    )
    try:
        with pytest.raises(VaultError) as error:
            await vault.project("canonical/example")
        assert error.value.category == f"authentication_{error_type.__name__}"
        assert len(attempts) == 1
        assert len(clients) == 2
        assert clients[0].is_closed
        assert "secret response details" not in str(error.value) + caplog.text
        # The next reconciliation can use a fresh pool without restarting the process.
        assert (await vault.project("canonical/example")).version == 3
    finally:
        await vault.aclose()


async def test_lookup_pool_recovery_preserves_valid_token() -> None:
    clients = []
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET" and len(clients) == 1:
            raise httpx.PoolTimeout("pool unavailable")
        return successful_response(request)

    def factory():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    vault = VaultClient(
        "https://vault.example", "role", "test-secret", "kv", "demos", client_factory=factory
    )
    try:
        assert (await vault.project("canonical/example")).version == 3
        assert [r.method for r in requests] == ["POST", "GET", "GET"]
        assert requests[1].headers["x-vault-token"] == requests[2].headers["x-vault-token"]
    finally:
        await vault.aclose()


async def test_renewal_pool_recovery_does_not_create_another_token() -> None:
    clients = []
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/renew-self") and len(clients) == 1:
            raise httpx.PoolTimeout("pool unavailable")
        return successful_response(request)

    def factory():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    vault = VaultClient(
        "https://vault.example", "role", "test-secret", "kv", "demos", client_factory=factory
    )
    try:
        await vault.project("canonical/example")
        vault._token_expires_at = 0
        await vault.project("canonical/example")
        assert sum(r.url.path.endswith("/login") for r in requests) == 1
        renewals = [r for r in requests if r.url.path.endswith("/renew-self")]
        assert len(renewals) == 2
        assert all(r.headers["x-vault-token"] == "test-token" for r in renewals)
    finally:
        await vault.aclose()


@pytest.mark.parametrize("status", [400, 403, 429, 500])
async def test_http_authentication_error_does_not_recycle_or_retry(status, caplog) -> None:
    clients = []
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(status, json={"errors": ["sensitive backend detail"]})

    def factory():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    vault = VaultClient(
        "https://vault.example", "role", "test-secret", "kv", "demos", client_factory=factory
    )
    try:
        with pytest.raises(VaultError) as error:
            await vault.project("canonical/example")
        assert error.value.category == f"authentication_HTTP_{status}"
        assert len(attempts) == len(clients) == 1
        assert "sensitive backend detail" not in str(error.value) + caplog.text
    finally:
        await vault.aclose()


async def test_injected_client_is_not_replaced_or_closed() -> None:
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.PoolTimeout("caller owns this transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        vault = VaultClient(
            "https://vault.example", "role", "test-secret", "kv", "demos", client=client
        )
        with pytest.raises(VaultError):
            await vault.project("canonical/example")
        await vault.aclose()
        assert len(attempts) == 1
        assert vault.client is client
        assert not client.is_closed


async def test_concurrent_requests_do_not_use_a_pool_during_replacement() -> None:
    clients = []

    async def failed(request):
        await asyncio.sleep(0)
        raise httpx.PoolTimeout("pool unavailable")

    def factory():
        handler = failed if not clients else successful_response
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    vault = VaultClient(
        "https://vault.example", "role", "test-secret", "kv", "demos", client_factory=factory
    )
    try:
        results = await asyncio.gather(*(vault.project("canonical/example") for _ in range(3)))
        assert all(project.version == 3 for project in results)
        assert len(clients) == 2
        assert clients[0].is_closed
    finally:
        await vault.aclose()


class FailedTLSStream(httpcore.AsyncNetworkStream):
    async def read(self, max_bytes, timeout=None):
        return b"HTTP/1.1 200 Connection established\r\n\r\n"

    async def write(self, buffer, timeout=None):
        pass

    async def aclose(self):
        pass

    async def start_tls(self, *args, **kwargs):
        raise httpcore.ConnectTimeout("simulated TLS failure; no real network")

    def get_extra_info(self, info):
        return None


class FailedTLSBackend(httpcore.AsyncNetworkBackend):
    async def connect_tcp(self, *args, **kwargs):
        return FailedTLSStream()


async def test_real_httpcore_proxy_tls_failure_cannot_poison_future_reconciliations() -> None:
    clients = []
    pools = []

    def factory():
        transport = httpx.AsyncHTTPTransport(
            proxy="http://proxy.invalid:3128", limits=httpx.Limits(max_connections=1)
        )
        # Exercise HTTPX/httpcore's real CONNECT and TLS state machine without sockets.
        transport._pool._network_backend = FailedTLSBackend()
        pools.append(transport._pool)
        client = httpx.AsyncClient(transport=transport)
        clients.append(client)
        return client

    vault = VaultClient(
        "https://vault.invalid", "role", "test-secret", "kv", "demos", client_factory=factory
    )
    try:
        for _ in range(2):
            with pytest.raises(VaultError) as error:
                await vault.project("canonical/example")
            assert error.value.category == "authentication_ConnectTimeout"
        # Exactly two attempts per reconciliation, not an unbounded retry loop.
        assert len(clients) == 5
        assert all(client.is_closed for client in clients[:-1])
        assert all(not pool.connections for pool in pools)
    finally:
        await vault.aclose()


async def test_validation_errors_do_not_disclose_secret_inputs(caplog) -> None:
    def handler(request):
        if request.method == "POST":
            return successful_response(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": {
                        "schema_version": 1,
                        "enabled": True,
                        "secrets": {"PRIVATE": {"invalid": "sensitive application value"}},
                    },
                    "metadata": {"version": 1},
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        vault = VaultClient(
            "https://vault.example", "role", "test-secret", "kv", "demos", client=client
        )
        with pytest.raises(VaultError) as error:
            await vault.project("canonical/example")
        assert error.value.category == "invalid_project_data"
        assert "sensitive application value" not in str(error.value) + caplog.text


async def test_ambiguous_renewal_failure_does_not_fall_through_to_login() -> None:
    clients = []
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/renew-self") and len(clients) == 1:
            raise httpx.ReadTimeout("renewal outcome is unknown")
        return successful_response(request)

    def factory():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    vault = VaultClient(
        "https://vault.example", "role", "test-secret", "kv", "demos", client_factory=factory
    )
    try:
        await vault.project("canonical/example")
        vault._token_expires_at = 0
        with pytest.raises(VaultError) as error:
            await vault.project("canonical/example")
        assert error.value.category == "authentication_ReadTimeout"
        assert len(requests) == 3
        assert sum(r.url.path.endswith("/login") for r in requests) == 1
        assert len(clients) == 2
        assert clients[0].is_closed
        await vault.project("canonical/example")
        assert sum(r.url.path.endswith("/login") for r in requests) == 1
    finally:
        await vault.aclose()


async def test_default_replacement_preserves_tls_proxy_and_exact_no_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,10.86.0.1")
    vault = VaultClient("https://vault.example", "role", "test-secret", "kv", "demos")
    previous = vault.client
    try:
        await vault._reset_client(httpx.PoolTimeout("test pool failure"))
        assert previous.is_closed
        for client in (previous, vault.client):
            proxied = client._transport_for_url(httpx.URL("https://vault.example"))
            assert isinstance(proxied._pool, httpcore.AsyncHTTPProxy)
            direct = client._transport_for_url(httpx.URL("https://10.86.0.1"))
            assert direct is client._transport
            assert direct._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert direct._pool._ssl_context.check_hostname
            assert client.timeout.pool == 5
    finally:
        await vault.aclose()
