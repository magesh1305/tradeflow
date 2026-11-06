"""FastAPI application: startup safety check, runtime (engine, journal, scheduler),
health, Kite login and recovery routes. The full control API arrives in Phase 7."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import factory
from app.api import auth, recovery
from app.config import get_settings
from app.core.clock import now_ist
from app.core.logging import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
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


app = FastAPI(title="tradeflow", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(recovery.router)


@app.get("/health")
async def health() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "mode": settings.mode, "time": now_ist().isoformat()}
