from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from demo_controller.models import ProjectConfig

logger = logging.getLogger(__name__)


class VaultError(RuntimeError):
    def __init__(self, message: str, *, category: str = "vault_error"):
        super().__init__(message)
        self.category = category


def _error_detail(error: Exception) -> str:
    # Exception strings and validation errors can contain credentials or secret inputs.
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


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
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.address = address.rstrip("/")
        self.role_id = role_id
        self.secret_id = secret_id
        self.kv_mount = kv_mount.strip("/")
        self.base_path = base_path.strip("/")
        self.namespace = namespace
        if client is not None and client_factory is not None:
            raise ValueError("provide either client or client_factory, not both")
        self._owns_client = client is None
        self._client_factory = client_factory or self._new_client
        self.client = client if client is not None else self._client_factory()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._renewable = False
        self._lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    @staticmethod
    def _new_client() -> httpx.AsyncClient:
        # Keep certificate validation and the Juju-provided proxy/NO_PROXY settings.
        return httpx.AsyncClient(timeout=httpx.Timeout(10, pool=5), verify=True, trust_env=True)

    async def _reset_client(self, error: httpx.TransportError) -> None:
        previous = self.client
        self.client = self._client_factory()
        logger.warning("Vault transport failed (%s); replacing HTTP client", type(error).__name__)
        try:
            await previous.aclose()
        except (httpx.HTTPError, OSError, RuntimeError) as close_error:
            logger.warning("Vault HTTP client cleanup failed (%s)", type(close_error).__name__)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # Replacement must not close a pool while another Vault request is using it.
        async with self._request_lock:
            for attempt in range(2):
                try:
                    return await self.client.request(method, url, **kwargs)
                except httpx.TransportError as error:
                    if not self._owns_client:
                        raise
                    # Failed proxy TLS setup can strand pool slots in httpcore 1.0.9.
                    # Discard the pool even when this request cannot safely be replayed.
                    await self._reset_client(error)
                    if attempt or not isinstance(
                        error,
                        (
                            httpx.PoolTimeout,
                            httpx.ConnectTimeout,
                            httpx.ConnectError,
                            httpx.ProxyError,
                        ),
                    ):
                        raise
                    # These failures occur before sending the request to Vault. Never
                    # replay a login/renewal after an ambiguous read/write failure.
                    await asyncio.sleep(0.1)
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        if self._owns_client:
            async with self._request_lock:
                await self.client.aclose()

    def _authentication_error(self, error: Exception) -> VaultError:
        detail = _error_detail(error)
        logger.warning("Vault authentication failed (%s)", detail)
        return VaultError(
            f"Vault authentication failed ({detail})",
            category=f"authentication_{detail.replace(' ', '_')}",
        )

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
                    response = await self._request(
                        "POST",
                        f"{self.address}/v1/auth/token/renew-self",
                        headers=self._headers(),
                    )
                    response.raise_for_status()
                    auth = response.json()["auth"]
                    self._set_token(auth)
                    return
                except httpx.TransportError as error:
                    raise self._authentication_error(error) from error
                except (httpx.HTTPError, KeyError, TypeError, ValueError):
                    self._token = None
            if not self.role_id or not self.secret_id:
                raise VaultError(
                    "Vault AppRole credentials are not configured", category="missing_credentials"
                )
            try:
                response = await self._request(
                    "POST",
                    f"{self.address}/v1/auth/approle/login",
                    headers=self._headers(authenticated=False),
                    json={"role_id": self.role_id, "secret_id": self.secret_id},
                )
                response.raise_for_status()
                self._set_token(response.json()["auth"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                raise self._authentication_error(error) from error

    def _set_token(self, auth: dict[str, Any]) -> None:
        token = auth.get("client_token")
        lease_duration = auth.get("lease_duration")
        if not isinstance(token, str) or not isinstance(lease_duration, int):
            raise VaultError(
                "Vault returned an invalid authentication response",
                category="invalid_authentication_response",
            )
        self._token = token
        self._token_expires_at = time.monotonic() + lease_duration
        self._renewable = bool(auth.get("renewable"))

    async def project(self, repository: str) -> VaultProject:
        await self._ensure_token()
        owner, name = repository.split("/", 1)
        path = f"{self.base_path}/v1/{owner}/{name}"
        try:
            url = f"{self.address}/v1/{self.kv_mount}/data/{path}"
            response = await self._request("GET", url, headers=self._headers())
            if response.status_code in {401, 403}:
                self._token = None
                self._token_expires_at = 0
                await self._ensure_token()
                response = await self._request("GET", url, headers=self._headers())
            if response.status_code == 404:
                raise VaultError(
                    "repository is not configured in Vault", category="project_not_configured"
                )
            response.raise_for_status()
            payload = response.json()["data"]
            config = ProjectConfig.model_validate(payload["data"])
            version = payload["metadata"]["version"]
            if not isinstance(version, int):
                raise TypeError("invalid KV version")
            return VaultProject(config=config, version=version)
        except VaultError:
            raise
        except httpx.HTTPError as error:
            detail = _error_detail(error)
            logger.warning("Vault project lookup failed for %s (%s)", repository, detail)
            raise VaultError(
                f"Vault project lookup failed ({detail})",
                category=f"lookup_{detail.replace(' ', '_')}",
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(
                "Vault project data is invalid for %s (%s)", repository, _error_detail(error)
            )
            raise VaultError(
                "Vault project returned invalid data", category="invalid_project_data"
            ) from error
