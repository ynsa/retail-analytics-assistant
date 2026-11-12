"""FastAPI app of the local test UI (spec 40, B2). Start it with `uv run opsfleet-agent ui`.

The server listens on 127.0.0.1 only. It serves one static page and a small JSON API. The answer
of a message comes as a Server-Sent Events stream: `stage` events while the assistant works, a
`turn` event with the answer, and `done` at the end.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from opsfleet_agent.ui.backend import UiBackend, UiError

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
KEEP_ALIVE_S = 15.0
# The page loads only its own files. The report charts are inline SVG in <img src="data:...">.
PAGE_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
# A report file has inline CSS and inline SVG, and no script.
REPORT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'"
)


class UserBody(BaseModel):
    user: str = Field(min_length=1, max_length=64)


class TextBody(UserBody):
    text: str = Field(max_length=20000)


class UndoBody(UserBody):
    batch_id: str | None = Field(default=None, max_length=32)


def create_app(backend: UiBackend, allowed_hosts: list[str] | None = None) -> FastAPI:
    app = FastAPI(
        title="Retail Analytics Assistant · local test UI",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # A web page on another site cannot reach this server through a DNS name (DNS rebinding).
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS
    )

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", PAGE_CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(UiError)
    async def ui_error(_: Request, exc: UiError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.get("/", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/users")
    def users() -> list[dict[str, Any]]:
        return backend.users()

    @app.post("/api/conversations")
    def start(body: UserBody) -> dict[str, Any]:
        return backend.start(body.user)

    @app.get("/api/conversations")
    def history(user: str) -> list[dict[str, Any]]:
        return backend.conversations(user)

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str, user: str) -> dict[str, Any]:
        return backend.conversation(user, conversation_id)

    @app.post("/api/conversations/{conversation_id}/messages")
    def message(conversation_id: str, body: TextBody) -> StreamingResponse:
        live = backend.begin_turn(body.user, conversation_id, body.text)
        return StreamingResponse(
            start_turn(backend, live, body.text),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post("/api/conversations/{conversation_id}/confirm")
    def confirm(conversation_id: str, body: TextBody) -> dict[str, Any]:
        return backend.confirm(body.user, conversation_id, body.text)

    @app.post("/api/conversations/{conversation_id}/cancel")
    def cancel(conversation_id: str, body: UserBody) -> dict[str, Any]:
        return backend.cancel(body.user, conversation_id)

    @app.post("/api/conversations/{conversation_id}/undo")
    def undo(conversation_id: str, body: UndoBody) -> dict[str, Any]:
        return backend.undo(body.user, conversation_id, body.batch_id)

    @app.get("/api/turns/{trace_id}")
    def steps(trace_id: str, user: str) -> dict[str, Any]:
        return backend.steps(user, trace_id)

    @app.get("/api/turns/{trace_id}/spans")
    def spans(trace_id: str, user: str) -> list[dict[str, Any]]:
        return backend.spans(user, trace_id)

    @app.get("/api/reports")
    def reports(user: str) -> list[dict[str, Any]]:
        return backend.reports(user)

    @app.get("/api/reports/{report_id}")
    def report(report_id: str, user: str) -> dict[str, Any]:
        return backend.report(user, report_id)

    @app.get("/api/reports/{report_id}/file")
    def report_file(report_id: str, user: str, format: str = "html") -> Response:
        text, name = backend.report_file(user, report_id, format)
        if format == "html":
            return HTMLResponse(text, headers={"Content-Security-Policy": REPORT_CSP})
        return Response(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return app


def start_turn(backend: UiBackend, live: Any, text: str) -> Iterator[str]:
    """Start the turn in a thread now, so the lock of the session is always released, also when
    the page closes before the stream starts. Yield its events as SSE. A comment line every
    15 s keeps the connection open during a long query."""
    events: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()

    def work() -> None:
        try:
            backend.run_turn(live, text, lambda e, d: events.put((e, d)))
        finally:
            events.put(None)

    threading.Thread(target=work, name="ui-turn", daemon=True).start()
    return _stream(events)


def _stream(events: queue.Queue[tuple[str, dict[str, Any]] | None]) -> Iterator[str]:
    while True:
        try:
            item = events.get(timeout=KEEP_ALIVE_S)
        except queue.Empty:
            yield ": working\n\n"
            continue
        if item is None:
            return
        event, data = item
        yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def serve(backend: UiBackend, host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(create_app(backend), host=host, port=port, log_level="warning")
