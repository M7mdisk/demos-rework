#!/usr/bin/env python3
"""Small HTTP fixture for local and controller demo tests."""

from __future__ import annotations

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in {"/healthz", "/readyz"}:
            self._json(200, {"status": "ok"})
            return
        if self.path != "/":
            self._json(404, {"error": "not found"})
            return

        secret = os.environ.get("SECRET_MESSAGE", "")
        self._json(
            200,
            {
                "message": os.environ.get(
                    "PLAIN_MESSAGE", "hello from the demo fixture"
                ),
                "revision": os.environ.get("DEMO_REVISION", "local"),
                "secret_configured": bool(secret),
                "secret_fingerprint": hashlib.sha256(secret.encode()).hexdigest()[:12]
                if secret
                else "",
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)

    def _json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
