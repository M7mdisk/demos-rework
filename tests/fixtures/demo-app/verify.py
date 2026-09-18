#!/usr/bin/env python3
"""Run the fixture locally and verify its public contract."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


def main() -> int:
    fixture = Path(__file__).parent
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = {
        **os.environ,
        "PORT": str(port),
        "PLAIN_MESSAGE": "plain fixture value",
        "SECRET_MESSAGE": "fixture secret",
        "DEMO_REVISION": "verification",
    }
    process = subprocess.Popen(
        [sys.executable, str(fixture / "app.py")],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(30):
            try:
                if get_json(f"http://127.0.0.1:{port}/readyz") == {"status": "ok"}:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("fixture did not become ready")

        response = get_json(f"http://127.0.0.1:{port}/")
        assert response["message"] == "plain fixture value"
        assert response["revision"] == "verification"
        assert response["secret_configured"] is True
        assert response["secret_fingerprint"]
        assert "fixture secret" not in json.dumps(response)
    finally:
        process.terminate()
        process.wait(timeout=5)
    print("fixture HTTP contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
