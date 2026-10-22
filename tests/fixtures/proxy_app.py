"""Local-only upstream for real Caddy protocol regression tests."""

import asyncio
import gzip

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import StreamingResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/login")
async def login(user: str):
    response = Response(status_code=302, headers={"Location": "/"})
    response.set_cookie("session", user, httponly=True, samesite="lax")
    response.set_cookie("csrf", "new-token", expires="Wed, 09 Jun 2038 10:18:14 GMT")
    return response


@app.get("/logout")
async def logout():
    response = Response(status_code=302, headers={"Location": "/"})
    response.delete_cookie("session")
    response.set_cookie("csrf", "replacement")
    return response


@app.get("/compressed")
async def compressed():
    return Response(
        gzip.compress(b"compressed workload body"), headers={"Content-Encoding": "gzip"}
    )


@app.get("/stream")
async def stream():
    async def chunks():
        yield b"first\n"
        await asyncio.sleep(0.05)
        yield b"last\n"

    return StreamingResponse(chunks(), media_type="text/plain")


@app.websocket("/socket")
async def socket(websocket: WebSocket):
    await websocket.accept()
    text = await websocket.receive_text()
    await websocket.send_text(text)
    await websocket.close()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def echo(path: str, request: Request):
    return {
        "user": request.cookies.get("session", "anonymous"),
        "host": request.headers.get("host"),
        "forwarded_host": request.headers.get("x-forwarded-host"),
        "forwarded_proto": request.headers.get("x-forwarded-proto"),
        "path": request.scope["raw_path"].decode(),
        "query": request.scope["query_string"].decode(),
        "method": request.method,
        "body": (await request.body()).decode(),
        "connection_header": request.headers.get("x-hop-only"),
    }
