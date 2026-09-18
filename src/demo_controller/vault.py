from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from demo_controller.models import ProjectConfig

logger = logging.getLogger(__name__)


class VaultError(RuntimeError):
    pass


@dataclass(frozen=True)
class VaultProject:
    config: ProjectConfig
    version: int


class VaultClient:
    def __init__(
        self,
        address: str,
        role_id: str,
        secret_id: str,
        kv_mount: str,
        base_path: str,
        namespace: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.address = address.rstrip("/")
        self.role_id = role_id
        self.secret_id = secret_id
        self.kv_mount = kv_mount.strip("/")
        self.base_path = base_path.strip("/")
        self.namespace = namespace
        self.client = client or httpx.AsyncClient(timeout=10, verify=True)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._renewable = False
        self._lock = asyncio.Lock()

    def _headers(self, *, authenticated: bool = True) -> dict[str, str]:
        headers = {"X-Vault-Namespace": self.namespace} if self.namespace else {}
        if authenticated and self._token:
            headers["X-Vault-Token"] = self._token
        return headers

    async def _ensure_token(self) -> None:
        async with self._lock:
            if self._token and time.monotonic() < self._token_expires_at - 30:
                return
            if self._token and self._renewable:
                try:
                    response = await self.client.post(
                        f"{self.address}/v1/auth/token/renew-self",
                        headers=self._headers(),
                    )
                    response.raise_for_status()
                    auth = response.json()["auth"]
                    self._set_token(auth)
                    return
                except (httpx.HTTPError, KeyError, TypeError, ValueError):
                    self._token = None
            if not self.role_id or not self.secret_id:
                raise VaultError("Vault AppRole credentials are not configured")
            try:
                response = await self.client.post(
                    f"{self.address}/v1/auth/approle/login",
                    headers=self._headers(authenticated=False),
                    json={"role_id": self.role_id, "secret_id": self.secret_id},
                )
                response.raise_for_status()
                self._set_token(response.json()["auth"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                detail = type(error).__name__
                if isinstance(error, httpx.HTTPStatusError):
                    detail = f"HTTP {error.response.status_code}"
                elif str(error):
                    detail = f"{detail}: {str(error)[:200]}"
                logger.warning("Vault authentication failed: %s", detail)
                raise VaultError(f"Vault authentication failed ({detail})") from error

    def _set_token(self, auth: dict[str, Any]) -> None:
        token = auth.get("client_token")
        lease_duration = auth.get("lease_duration")
        if not isinstance(token, str) or not isinstance(lease_duration, int):
            raise VaultError("Vault returned an invalid authentication response")
        self._token = token
        self._token_expires_at = time.monotonic() + lease_duration
        self._renewable = bool(auth.get("renewable"))

    async def project(self, repository: str) -> VaultProject:
        await self._ensure_token()
        owner, name = repository.split("/", 1)
        path = f"{self.base_path}/v1/{owner}/{name}"
        try:
            url = f"{self.address}/v1/{self.kv_mount}/data/{path}"
            response = await self.client.get(url, headers=self._headers())
            if response.status_code in {401, 403}:
                self._token = None
                self._token_expires_at = 0
                await self._ensure_token()
                response = await self.client.get(url, headers=self._headers())
            if response.status_code == 404:
                raise VaultError("repository is not configured in Vault")
            response.raise_for_status()
            payload = response.json()["data"]
            config = ProjectConfig.model_validate(payload["data"])
            version = payload["metadata"]["version"]
            if not isinstance(version, int):
                raise TypeError("invalid KV version")
            return VaultProject(config=config, version=version)
        except VaultError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            logger.warning("Vault project lookup failed for %s", repository)
            raise VaultError("Vault project lookup failed or returned invalid data") from error
