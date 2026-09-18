from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    namespace: str
    hostname_suffix: str
    controller_owner: str
    image_registry: str
    vault_address: str
    vault_namespace: str | None
    vault_kv_mount: str
    vault_base_path: str
    vault_role_id: str
    vault_secret_id: str
    hmac_credentials: dict[str, str]
    max_demo_lifetime_seconds: int
    reconcile_interval_seconds: float
    request_max_bytes: int
    signature_max_age_seconds: int
    rate_limit_per_minute: int
    proxy_timeout_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        credentials = json.loads(os.getenv("HMAC_CREDENTIALS", "{}"))
        if not isinstance(credentials, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in credentials.items()
        ):
            raise ValueError("HMAC_CREDENTIALS must be a JSON string map")
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "/data/controller.db")),
            namespace=os.getenv("KUBERNETES_NAMESPACE", "default"),
            hostname_suffix=os.getenv("HOSTNAME_SUFFIX", "demos.canonical.com").strip("."),
            controller_owner=os.getenv("CONTROLLER_OWNER", "demo-controller"),
            image_registry=os.getenv("IMAGE_REGISTRY", "ghcr.io").strip().strip("/").lower(),
            vault_address=os.getenv("VAULT_ADDRESS", "https://vault.ps7.admin.canonical.com"),
            vault_namespace=os.getenv("VAULT_NAMESPACE") or None,
            vault_kv_mount=os.getenv("VAULT_KV_MOUNT", "secret").strip("/"),
            vault_base_path=os.getenv("VAULT_BASE_PATH", "demos").strip("/"),
            vault_role_id=os.getenv("VAULT_ROLE_ID", ""),
            vault_secret_id=os.getenv("VAULT_SECRET_ID", ""),
            hmac_credentials=credentials,
            max_demo_lifetime_seconds=int(os.getenv("MAX_DEMO_LIFETIME", "604800")),
            reconcile_interval_seconds=float(os.getenv("RECONCILE_INTERVAL", "10")),
            request_max_bytes=int(os.getenv("REQUEST_MAX_BYTES", "65536")),
            signature_max_age_seconds=int(os.getenv("SIGNATURE_MAX_AGE", "300")),
            rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")),
            proxy_timeout_seconds=float(os.getenv("PROXY_TIMEOUT", "30")),
        )
