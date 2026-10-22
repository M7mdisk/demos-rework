from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from demo_controller.auth import Authenticator, request_hash
from demo_controller.models import DeployRequest, DestroyRequest
from demo_controller.reconciler import ReconciliationLoop
from demo_controller.settings import Settings
from demo_controller.store import Store

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    store: Store,
    authenticator: Authenticator,
    reconciliation: ReconciliationLoop,
) -> FastAPI:
    task: asyncio.Task[None] | None = None

    def report_loop_exit(completed: asyncio.Task[None]) -> None:
        if not completed.cancelled() and (error := completed.exception()) is not None:
            logger.error("Reconciliation loop stopped (%s)", type(error).__name__)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal task
        task = asyncio.create_task(reconciliation.run())
        task.add_done_callback(report_loop_exit)
        try:
            yield
        finally:
            reconciliation.stop()
            # Loop failures are reported by the callback and /statusz.
            await asyncio.gather(task, return_exceptions=True)

    # Caddy routes only the controller hostname here; demo traffic never
    # enters FastAPI, including paths such as /docs, /readyz, and /api/v1/.
    app = FastAPI(
        title="Canonical demos controller",
        version="1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

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
        # API readiness only; Caddy answers ingress readiness independently
        # so a database/control-plane failure does not withdraw retained demos.
        try:
            store.list_reconcilable()
        except sqlite3.Error as error:
            raise HTTPException(503, "state database unavailable") from error
        return {"status": "ready"}

    @app.get("/statusz")
    async def controller_status() -> JSONResponse:
        dependencies = reconciliation.health()
        running = task is not None and not task.done()
        database = "ok"
        counts: Counter[str] = Counter()
        try:
            counts.update(record.state for record in store.list_reconcilable())
        except sqlite3.Error:
            database = "error"
        degraded = (
            not running
            or database != "ok"
            or bool(counts["failed"])
            or any(item["status"] == "error" for item in dependencies.values())
        )
        return JSONResponse(
            {
                "status": "degraded" if degraded else "ok",
                "database": database,
                "reconciliation": {
                    "state": "running" if running else "stopped",
                    "last_completed_at": reconciliation.last_completed_at,
                },
                "dependencies": dependencies,
                "demos": {
                    state: counts[state]
                    for state in ("pending", "reconciling", "ready", "failed", "deleting")
                },
            },
            status_code=503 if degraded else 200,
        )

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
            record, _ = store.upsert_deploy(payload, request_hash(body))
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

    return app
