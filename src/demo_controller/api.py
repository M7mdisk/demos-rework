from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from demo_controller.auth import Authenticator, request_hash
from demo_controller.models import DeployRequest, DestroyRequest
from demo_controller.naming import resource_name
from demo_controller.reconciler import ReconciliationLoop
from demo_controller.settings import Settings
from demo_controller.store import Store


def create_app(
    settings: Settings,
    store: Store,
    authenticator: Authenticator,
    reconciliation: ReconciliationLoop,
    proxy_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(reconciliation.run())
        yield
        reconciliation.stop()
        await task

    app = FastAPI(title="Canonical demos controller", version="1.0", lifespan=lifespan)
    client = proxy_client or httpx.AsyncClient(timeout=settings.proxy_timeout_seconds)

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        if request.url.path not in {"/api/v1/deploy", "/api/v1/destroy"}:
            return await call_next(request)
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > settings.request_max_bytes:
                    return JSONResponse({"detail": "request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "invalid content-length"}, status_code=400)
        body = await request.body()
        if len(body) > settings.request_max_bytes:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)

    async def authenticated_repository(request: Request) -> str:
        return await authenticator.authenticate(request)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        store.list_reconcilable()
        return {"status": "ready"}

    @app.post("/api/v1/deploy", status_code=status.HTTP_202_ACCEPTED)
    async def deploy(
        request: Request, repository: str = Depends(authenticated_repository)
    ) -> Response:
        body = await request.body()
        try:
            payload = DeployRequest.model_validate_json(body)
        except (ValueError, json.JSONDecodeError) as error:
            raise HTTPException(422, "invalid deploy request") from error
        if payload.repository != repository:
            raise HTTPException(403, "credential does not authorize repository")
        if not payload.image.lower().startswith(
            f"{settings.image_registry}/{payload.repository}@sha256:"
        ):
            raise HTTPException(403, "image registry is not authorized")
        if request.headers.get("X-Demos-Nonce") != payload.delivery_id:
            raise HTTPException(400, "nonce must equal delivery_id")
        try:
            record, _ = store.upsert_deploy(
                payload, request_hash(body), settings.max_demo_lifetime_seconds
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return JSONResponse(store.status(record).model_dump(), status_code=202)

    @app.post("/api/v1/destroy", status_code=status.HTTP_202_ACCEPTED)
    async def destroy(
        request: Request, repository: str = Depends(authenticated_repository)
    ) -> Response:
        body = await request.body()
        try:
            payload = DestroyRequest.model_validate_json(body)
        except (ValueError, json.JSONDecodeError) as error:
            raise HTTPException(422, "invalid destroy request") from error
        if payload.repository != repository:
            raise HTTPException(403, "credential does not authorize repository")
        if request.headers.get("X-Demos-Nonce") != payload.delivery_id:
            raise HTTPException(400, "nonce must equal delivery_id")
        try:
            record = store.request_destroy(
                payload.repository, payload.pr, payload.delivery_id, request_hash(body)
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return JSONResponse(store.status(record).model_dump(), status_code=202)

    @app.get("/api/v1/demos/{owner}/{name}/{pr}")
    async def demo_status(
        owner: str,
        name: str,
        pr: int,
        repository: str = Depends(authenticated_repository),
    ) -> dict[str, object]:
        requested = f"{owner}/{name}".lower()
        if requested != repository:
            raise HTTPException(403, "credential does not authorize repository")
        record = store.get(requested, pr)
        if not record:
            raise HTTPException(404, "demo not found")
        return store.status(record).model_dump()

    @app.api_route(
        "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def proxy(path: str, request: Request) -> Response:
        requested_host = request.headers.get("host", "").split(":", 1)[0].lower().rstrip(".")
        if not requested_host.endswith(f".{settings.hostname_suffix}"):
            raise HTTPException(404, "unknown demo hostname")
        record = store.get_ready_by_hostname(requested_host)
        if not record or not record.port:
            raise HTTPException(404, "demo is not active")
        service = resource_name(record.repository, record.pr)
        target = f"http://{service}.{settings.namespace}.svc.cluster.local:{record.port}/{path}"
        if request.url.query:
            target += f"?{request.url.query}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }
        headers["x-forwarded-host"] = requested_host
        try:
            upstream = await client.request(
                request.method,
                target,
                content=await request.body(),
                headers=headers,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise HTTPException(503, "demo backend unavailable") from error
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in {"content-length", "connection", "transfer-encoding"}
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=upstream.headers.get("content-type"),
        )

    return app
