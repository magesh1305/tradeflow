"""FastAPI application: startup safety check, runtime (engine, journal, scheduler),
control API and status WebSocket.

Auth: everything except /health and /auth/* requires X-Admin-Token (constant-time
compare). /ws takes the token as a query parameter; access logs redact it.
CORS: only FRONTEND_ORIGIN may call the API from a browser.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import factory
from app.api import auth, connection, control, recovery, ws
from app.api import settings as plan_settings
from app.config import get_settings
from app.core.clock import now_ist
from app.core.errors import DATABASE_DOWN, is_database_error
from app.core.logging import configure_logging

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"(token=)[^&\s\"]+")


class RedactTokenFilter(logging.Filter):
    """Removes ?token=... from access-log lines (the WebSocket auth token)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                _TOKEN_RE.sub(r"\1***", a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_RE.sub(r"\1***", record.msg)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(RedactTokenFilter())
    settings.assert_safe()  # refuses to start live unless every latch is set
    # Tests may pre-install app.state.auth with mocks; don't overwrite it.
    if getattr(app.state, "auth", None) is None and settings.kite_api_key:
        app.state.auth = factory.build_auth(settings, factory.build_token_store(settings))
    runtime = None
    if settings.engine_autostart:
        runtime = factory.build_runtime(settings, getattr(app.state, "auth", None))
        app.state.runtime = runtime
        await runtime.startup()
    log.info("tradeflow started", extra={"mode": settings.mode, "feed": settings.feed})
    try:
        yield
    finally:
        if runtime is not None:
            await runtime.shutdown()
            app.state.runtime = None
        log.info("tradeflow stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    docs = settings.api_docs  # unauthenticated, so off by default
    app = FastAPI(
        title="tradeflow",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["X-Admin-Token", "Content-Type"],
    )
    app.include_router(auth.router)  # public: the daily Kite login
    app.include_router(recovery.router)  # admin
    app.include_router(control.router)  # admin
    app.include_router(plan_settings.router)  # admin: the daily plan
    app.include_router(connection.router)  # admin: read-only connection check
    app.include_router(ws.router)  # token query param

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Always answer with JSON the dashboard can show, never a bare 500 page.
        if is_database_error(exc):
            log.warning("database unavailable on %s %s: %s", request.method, request.url.path, exc)
            return JSONResponse({"detail": DATABASE_DOWN}, status_code=503)
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            {"detail": "Unexpected server error. Details are in the backend log."},
            status_code=500,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": get_settings().mode, "time": now_ist().isoformat()}

    return app


app = create_app()
