#!/usr/bin/env python3
"""Minimal Vault-compatible server for local controller integration tests."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT = {
    "schema_version": 1,
    "enabled": True,
    "environment": {
        "PLAIN_MESSAGE": "restart recovery fixture",
        "DEMO_REVISION": "persistent-state",
    },
    "secrets": {"SECRET_MESSAGE": "fixture-secret-not-for-output"},
    "port": 8000,
    "health_path": "/readyz",
    "resources": {
        "cpu_request": "50m",
        "cpu_limit": "1",
        "memory_request": "64Mi",
        "memory_limit": "256Mi",
    },
    "image_namespace": "canonical/webteam-juju-demos-testing",
    "lifetime_seconds": 3600,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/v1/sys/health":
            self._json(200, {"initialized": True, "sealed": False})
            return
        if self.path == ("/v1/kv/data/demos/v1/canonical/webteam-juju-demos-testing"):
            if self.headers.get("X-Vault-Token") != "local-fixture-token":
                self._json(403, {"errors": ["permission denied"]})
                return
            self._json(
                200,
                {"data": {"data": PROJECT, "metadata": {"version": 1}}},
            )
            return
        self._json(404, {"errors": ["not found"]})

    def do_POST(self) -> None:
        if self.path in {
            "/v1/auth/approle/login",
            "/v1/auth/token/renew-self",
        }:
            self._discard_body()
            self._json(
                200,
                {
                    "auth": {
                        "client_token": "local-fixture-token",
                        "lease_duration": 300,
                        "renewable": True,
                    }
                },
            )
            return
        self._json(404, {"errors": ["not found"]})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _discard_body(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)

    def _json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18200)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
