from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from demo_controller.api import _ProxyCookieJar, create_app
from demo_controller.models import DeployRequest
from demo_controller.settings import Settings
from demo_controller.store import Store


@pytest.fixture
def proxy_app(tmp_path):
    store = Store(tmp_path / "state.db", "demos.example")
    for pr in (1, 2):
        store.upsert_deploy(
            DeployRequest(
                repository="canonical/example",
                pr=pr,
                commit_sha="a" * 40,
                image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
                delivery_id=f"delivery-{pr}",
            ),
            f"hash-{pr}",
        )
        store.update_state("canonical/example", pr, "ready", "", port=8000)
    settings = MagicMock(spec=Settings)
    settings.hostname_suffix = "demos.example"
    settings.namespace = "testing"
    settings.proxy_timeout_seconds = 3

    def build(client):
        return create_app(settings, store, MagicMock(), MagicMock(), proxy_client=client)

    return build


@pytest.mark.parametrize("visitor_pr", [1, 2])
async def test_login_cookie_is_not_reused_for_another_visitor(proxy_app, visitor_pr):
    received = []

    def upstream(request):
        received.append(request.headers.get("cookie"))
        if request.url.path == "/login":
            return httpx.Response(
                302,
                headers={"Location": "/", "Set-Cookie": "session=alice; Path=/; HttpOnly"},
            )
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as proxy:
        # Even cookies in an injected client's jar must never reach a browser's request.
        proxy.cookies.set("client_cookie", "must-not-leak")
        app = proxy_app(proxy)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://example-pr-1.demos.example",
        ) as alice:
            response = await alice.get("/login")
            assert response.status_code == 302
            assert response.headers["location"] == "/"
            assert alice.cookies.get("session") == "alice"
            await alice.get("/")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"https://example-pr-{visitor_pr}.demos.example",
        ) as visitor:
            assert (await visitor.get("/")).status_code == 200

    assert received == [None, "session=alice", None]


async def test_concurrent_visitors_forward_only_their_own_cookies(proxy_app):
    arrived = 0
    all_arrived = asyncio.Event()

    async def upstream(request):
        nonlocal arrived
        arrived += 1
        if arrived == 3:
            all_arrived.set()
        await asyncio.wait_for(all_arrived.wait(), timeout=5)
        cookie = request.headers.get("cookie", "anonymous")
        return httpx.Response(200, text=cookie, headers={"Set-Cookie": "session=other; Path=/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as proxy:
        proxy.cookies.set("session", "stale-shared-session")
        app = proxy_app(proxy)

        async def visit(cookie):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://example-pr-1.demos.example",
            ) as browser:
                return await browser.get("/", headers={"Cookie": cookie} if cookie else {})

        responses = await asyncio.gather(visit("session=alice"), visit("session=bob"), visit(None))

    assert [response.text for response in responses] == [
        "session=alice",
        "session=bob",
        "anonymous",
    ]


async def test_multiple_set_cookie_headers_and_logout_are_preserved(proxy_app):
    cookies = [
        "session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
        "csrf=new-token; Path=/; Expires=Wed, 09 Jun 2038 10:18:14 GMT; Secure",
    ]

    def upstream(request):
        assert request.headers["cookie"] == "session=alice; csrf=old-token"
        return httpx.Response(
            302,
            headers=[("Location", "/"), *(("Set-Cookie", cookie) for cookie in cookies)],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as proxy:
        app = proxy_app(proxy)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://example-pr-1.demos.example",
        ) as browser:
            browser.cookies.set("session", "alice", domain="example-pr-1.demos.example", path="/")
            browser.cookies.set("csrf", "old-token", domain="example-pr-1.demos.example", path="/")
            response = await browser.get("/logout")
            assert response.status_code == 302
            assert response.headers.get_list("set-cookie") == cookies
            assert browser.cookies.get("session") is None
            assert browser.cookies.get("csrf") == "new-token"


async def test_proxy_cookie_jar_does_not_retain_workload_sessions():
    async with httpx.AsyncClient(
        cookies=_ProxyCookieJar(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"Set-Cookie": "session=alice; Path=/"})
        ),
    ) as proxy:
        await proxy.send(httpx.Request("GET", "http://workload.example/"))
        assert list(proxy.cookies.jar) == []
