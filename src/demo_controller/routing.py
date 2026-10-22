"""Caddy configuration management; no application traffic passes through Python."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from demo_controller.naming import hostname, resource_name
from demo_controller.store import DemoRecord

ADMIN_ADDRESS = "127.0.0.1:2019"


def proxy_route(host: str, upstream: str) -> dict:
    return {
        "match": [{"host": [host]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": upstream}],
                "headers": {
                    "request": {
                        "set": {
                            "X-Forwarded-Host": ["{http.request.host}"],
                            # Public traffic arrives exclusively through the TLS
                            # ingress. Do not trust a browser-supplied scheme.
                            "X-Forwarded-Proto": ["https"],
                        }
                    }
                },
            }
        ],
        "terminal": True,
    }


def render_config(
    api_hostname: str, namespace: str, suffix: str, records: list[DemoRecord]
) -> dict:
    routes = [
        # HAProxy's HTTP health probe has no Host header. Pebble uses localhost.
        # Do not intercept /readyz (or any controller path) on a demo hostname.
        {
            "match": [{"host": ["", "localhost", "127.0.0.1"], "path": ["/readyz"]}],
            "handle": [{"handler": "static_response", "status_code": 200}],
            "terminal": True,
        },
        proxy_route(api_hostname, "127.0.0.1:8081"),
    ]
    now = int(time.time())
    for record in sorted(records, key=lambda item: (item.repository, item.pr)):
        if (
            record.desired_present
            and record.port is not None
            and record.state in {"pending", "reconciling", "ready", "failed"}
            and record.expires_at > now
        ):
            routes.append(
                proxy_route(
                    hostname(record.repository, record.pr, suffix),
                    f"{resource_name(record.repository, record.pr)}.{namespace}.svc.cluster.local:"
                    f"{record.port}",
                )
            )
    routes.append({"handle": [{"handler": "static_response", "status_code": 404}]})
    return {
        "admin": {"listen": ADMIN_ADDRESS},
        "apps": {
            "http": {
                "servers": {
                    "demos": {
                        "listen": [":8080"],
                        "automatic_https": {"disable": True},
                        "routes": routes,
                    }
                }
            }
        },
    }


class CaddyRoutes:
    def __init__(self, api_hostname: str, namespace: str, suffix: str):
        self.api_hostname = api_hostname
        self.namespace = namespace
        self.suffix = suffix
        # Administration is local only and must never traverse the egress proxy.
        self.client = httpx.AsyncClient(
            base_url=f"http://{ADMIN_ADDRESS}", trust_env=False, timeout=5
        )

    async def sync(self, records: list[DemoRecord]) -> None:
        config = render_config(self.api_hostname, self.namespace, self.suffix, records)
        # /load provisions the entire config atomically. Invalid updates leave
        # the last working configuration running. Identical configs are no-ops.
        response = await self.client.post("/load", json=config)
        response.raise_for_status()

    async def aclose(self) -> None:
        await self.client.aclose()


def run() -> None:
    """Pebble entry point: bootstrap once, then resume Caddy's durable config."""
    path = Path("/data/caddy-bootstrap.json")
    path.write_text(
        json.dumps(
            render_config(
                os.environ["API_HOSTNAME"],
                os.environ["KUBERNETES_NAMESPACE"],
                os.environ["HOSTNAME_SUFFIX"],
                [],
            )
        )
    )
    # XDG_CONFIG_HOME=/data makes Caddy's autosave survive container restarts.
    # --resume uses it when present, falling back to --config on first boot.
    os.execv("/usr/bin/caddy", ["caddy", "run", "--resume", "--config", str(path)])


if __name__ == "__main__":
    run()
