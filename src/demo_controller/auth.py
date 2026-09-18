from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from demo_controller.store import Store


class Authenticator:
    def __init__(
        self,
        credentials: dict[str, str],
        store: Store,
        max_age_seconds: int,
        rate_limit_per_minute: int,
    ):
        self.credentials = {repository.lower(): key for repository, key in credentials.items()}
        self.store = store
        self.max_age_seconds = max_age_seconds
        self.rate_limit = rate_limit_per_minute
        self._requests: dict[str, deque[int]] = defaultdict(deque)

    async def authenticate(self, request: Request) -> str:
        repository = request.headers.get("X-Demos-Repository", "").lower()
        timestamp_text = request.headers.get("X-Demos-Timestamp", "")
        nonce = request.headers.get("X-Demos-Nonce", "")
        supplied = request.headers.get("X-Demos-Signature", "")
        key = self.credentials.get(repository)
        if not key:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown repository credential")
        try:
            timestamp = int(timestamp_text)
        except ValueError:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid signature timestamp"
            ) from None
        now = int(time.time())
        if abs(now - timestamp) > self.max_age_seconds:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "stale signature")
        body = await request.body()
        canonical = b"\n".join(
            (
                request.method.upper().encode(),
                request.url.path.encode(),
                timestamp_text.encode(),
                nonce.encode(),
                body,
            )
        )
        expected = hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()
        normalized = supplied.removeprefix("sha256=")
        if not hmac.compare_digest(expected, normalized):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
        self._check_rate(repository, now)
        request_fingerprint = b"\n".join(
            (
                repository.encode(),
                request.method.upper().encode(),
                request.url.path.encode(),
                nonce.encode(),
                body,
            )
        )
        fingerprint = hashlib.sha256(request_fingerprint).hexdigest()
        if not nonce or not self.store.use_nonce(
            repository,
            nonce,
            fingerprint,
            now,
            self.max_age_seconds * 2,
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "replayed delivery")
        return repository

    def _check_rate(self, repository: str, now: int) -> None:
        requests = self._requests[repository]
        while requests and requests[0] <= now - 60:
            requests.popleft()
        if len(requests) >= self.rate_limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
        requests.append(now)


def request_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
