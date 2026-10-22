from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from demo_controller.routing import CaddyRoutes, render_config
from demo_controller.store import DemoRecord


def record(pr=1, **changes):
    return replace(
        DemoRecord(
            repository="canonical/example",
            pr=pr,
            commit_sha="a" * 40,
            image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
            delivery_id=f"delivery-{pr}",
            request_hash="hash",
            desired_present=True,
            state="ready",
            vault_version=1,
            port=8000,
            message="ready",
            created_at=int(time.time()),
            updated_at=int(time.time()),
            expires_at=int(time.time()) + 3600,
        ),
        **changes,
    )


def test_routes_are_exact_and_keep_previously_ready_backends():
    records = [
        record(),
        record(2, state="pending"),
        record(3, state="failed"),
        record(4, state="reconciling"),
        record(5, port=None),
        record(6, desired_present=False),
        record(7, expires_at=0),
        record(8, state="deleting"),
    ]
    config = render_config("api.demos.example", "testing", "demos.example", records)
    server = config["apps"]["http"]["servers"]["demos"]
    routes = server["routes"]
    assert config["admin"] == {"listen": "127.0.0.1:2019"}
    assert server["automatic_https"] == {"disable": True}
    assert server["listen"] == [":8080"]
    assert routes[1]["match"] == [{"host": ["api.demos.example"]}]
    assert routes[1]["handle"][0]["upstreams"] == [{"dial": "127.0.0.1:8081"}]
    assert [route["match"][0]["host"][0] for route in routes[2:-1]] == [
        f"example-pr-{pr}.demos.example" for pr in range(1, 5)
    ]
    assert routes[2]["handle"][0]["upstreams"][0]["dial"].endswith(
        ".testing.svc.cluster.local:8000"
    )
    assert routes[-1] == {"handle": [{"handler": "static_response", "status_code": 404}]}
    assert config == render_config("api.demos.example", "testing", "demos.example", records[::-1])


async def test_admin_updates_use_atomic_load_and_ignore_egress_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-use.invalid:3128")
    routes = CaddyRoutes("api.demos.example", "testing", "demos.example")
    assert routes.client.trust_env is False
    seen = []

    def admin(request):
        seen.append(request)
        return httpx.Response(200)

    await routes.client.aclose()
    routes.client = httpx.AsyncClient(
        transport=httpx.MockTransport(admin), base_url="http://127.0.0.1:2019", trust_env=False
    )
    await routes.sync([record()])
    await routes.sync([record()])
    await routes.aclose()
    assert len(seen) == 2  # Re-send to recover a restarted Caddy, not a Python cache.
    assert all(request.method == "POST" and request.url.path == "/load" for request in seen)
    assert json.loads(seen[0].content)["admin"]["listen"] == "127.0.0.1:2019"
    assert routes.client.is_closed


async def test_rejected_admin_update_is_not_silently_successful():
    routes = CaddyRoutes("api.demos.example", "testing", "demos.example")
    routes.client.post = AsyncMock(
        return_value=httpx.Response(400, request=httpx.Request("POST", "http://localhost/load"))
    )
    with pytest.raises(httpx.HTTPStatusError):
        await routes.sync([record()])
    await routes.aclose()


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_http(url, process, log):
    with httpx.Client(trust_env=False, timeout=0.2) as client:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail(f"Local test process exited: {log.read_text()}")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    pytest.fail(f"Local test process did not become ready: {log.read_text()}")


@pytest.fixture
def caddy(tmp_path):
    binary = os.getenv("CADDY_BINARY") or shutil.which("caddy")
    if not binary:
        pytest.skip("Set CADDY_BINARY or install Caddy to run real proxy protocol tests")
    port, admin_port, upstream_port = free_port(), free_port(), free_port()
    config = render_config("api.demos.example", "testing", "demos.example", [record(), record(2)])
    config["admin"]["listen"] = f"127.0.0.1:{admin_port}"
    server = config["apps"]["http"]["servers"]["demos"]
    server["listen"] = [f"127.0.0.1:{port}"]
    # Keep the generated host matching/HTTP behavior but use a local upstream.
    for route in server["routes"][1:-1]:
        route["handle"][0]["upstreams"] = [{"dial": f"127.0.0.1:{upstream_port}"}]
    path = tmp_path / "bootstrap.json"
    path.write_text(json.dumps(config))
    env = os.environ | {"XDG_CONFIG_HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path)}
    log = tmp_path / "process.log"
    processes = []
    with log.open("w") as output:
        upstream = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "proxy_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(upstream_port),
                "--app-dir",
                str(Path(__file__).parent / "fixtures"),
            ],
            stdout=output,
            stderr=output,
        )
        processes.append(upstream)
        try:
            wait_for_http(f"http://127.0.0.1:{upstream_port}/", upstream, log)

            def start():
                process = subprocess.Popen(
                    [binary, "run", "--resume", "--config", str(path)],
                    env=env,
                    stdout=output,
                    stderr=output,
                )
                processes.append(process)
                wait_for_http(f"http://127.0.0.1:{port}/readyz", process, log)
                return process

            process = start()
            with httpx.Client(trust_env=False, base_url=f"http://127.0.0.1:{admin_port}") as admin:
                yield {
                    "url": f"http://127.0.0.1:{port}",
                    "admin": admin,
                    "config": config,
                    "process": process,
                    "start": start,
                    "upstream": upstream,
                }
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


def test_real_proxy_isolates_sessions_and_preserves_login_logout(caddy):
    with httpx.Client(
        base_url=caddy["url"], trust_env=False, headers={"Host": "example-pr-1.demos.example"}
    ) as alice:
        response = alice.get("/login?user=alice")
        assert response.status_code == 302
        assert response.headers["location"] == "/"
        assert len(response.headers.get_list("set-cookie")) == 2
        assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
        assert "SameSite=lax" in response.headers.get_list("set-cookie")[0]
        assert "Wed, 09 Jun 2038" in response.headers.get_list("set-cookie")[1]
        assert alice.get("/").json()["user"] == "alice"
        with httpx.Client(base_url=caddy["url"], trust_env=False) as visitor:
            for host in ("example-pr-1.demos.example", "example-pr-2.demos.example"):
                assert visitor.get("/", headers={"Host": host}).json()["user"] == "anonymous"
        response = alice.get("/logout")
        assert len(response.headers.get_list("set-cookie")) == 2
        assert alice.get("/").json()["user"] == "anonymous"


async def test_real_proxy_concurrent_visitors(caddy):
    async def visit(user):
        async with httpx.AsyncClient(
            base_url=caddy["url"], trust_env=False, headers={"Host": "example-pr-1.demos.example"}
        ) as browser:
            if user != "anonymous":
                await browser.get(f"/login?user={user}")
            return (await browser.get("/")).json()["user"]

    users = ["alice", "bob", "anonymous"] * 5
    assert await asyncio.gather(*(visit(user) for user in users)) == users


def test_real_proxy_preserves_http_and_never_shadows_demo_paths(caddy):
    host = "example-pr-1.demos.example"
    with httpx.Client(base_url=caddy["url"], trust_env=False, headers={"Host": host}) as browser:
        for path in ("/docs", "/redoc", "/openapi.json", "/readyz", "/statusz", "/api/v1/deploy"):
            assert browser.get(path).json()["path"] == path
        response = browser.patch(
            "/a%2Fb?next=%2Flogin&x=1&x=2",
            content=b"payload",
            headers={
                "X-Forwarded-Proto": "http",
                "Connection": "X-Hop-Only",
                "X-Hop-Only": "must-not-forward",
            },
        )
        data = response.json()
        assert data == {
            "user": "anonymous",
            "host": host,
            "forwarded_host": host,
            "forwarded_proto": "https",
            "path": "/a%2Fb",
            "query": "next=%2Flogin&x=1&x=2",
            "method": "PATCH",
            "body": "payload",
            "connection_header": None,
        }
        assert browser.get("/compressed").content == b"compressed workload body"
        assert browser.get("/stream").content == b"first\nlast\n"
        for unknown in (
            "other.example",
            "unknown.demos.example",
            "example-pr-1.demos.example.attacker.invalid",
        ):
            response = browser.get("/", headers={"Host": unknown})
            assert response.status_code == 404
            assert response.content == b""


def test_real_proxy_websocket(caddy):
    from websockets.sync.client import connect

    port = int(caddy["url"].rsplit(":", 1)[1])
    with (
        socket.create_connection(("127.0.0.1", port)) as connection,
        connect("ws://example-pr-1.demos.example/socket", sock=connection, proxy=None) as websocket,
    ):
        websocket.send("hello demo")
        assert websocket.recv() == "hello demo"


def test_real_proxy_keeps_config_after_rejection_and_restart(caddy):
    config = caddy["config"]
    config["apps"]["http"]["servers"]["demos"]["routes"][2]["match"] = [
        {"host": ["changed.demos.example"]}
    ]
    config["apps"]["http"]["servers"]["demos"]["routes"][1]["handle"][0]["upstreams"] = [
        {"dial": f"127.0.0.1:{free_port()}"}
    ]
    assert caddy["admin"].post("/load", json=config).status_code == 200
    assert caddy["admin"].post("/load", json={"apps": {"invalid_module": {}}}).status_code >= 400
    caddy["process"].terminate()
    caddy["process"].wait(timeout=10)
    caddy["start"]()
    with httpx.Client(base_url=caddy["url"], trust_env=False) as browser:
        assert browser.get("/", headers={"Host": "changed.demos.example"}).status_code == 200
        assert browser.get("/", headers={"Host": "example-pr-1.demos.example"}).status_code == 404
        # Health and retained routes do not depend on the Python control plane.
        assert browser.get("/readyz").status_code == 200
        assert browser.get("/", headers={"Host": "api.demos.example"}).status_code == 502
        assert browser.get("/", headers={"Host": "changed.demos.example"}).status_code == 200

    port = int(caddy["url"].rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port)) as probe:
        probe.sendall(b"GET /readyz HTTP/1.0\r\n\r\n")
        assert probe.recv(4096).startswith(b"HTTP/1.0 200")
