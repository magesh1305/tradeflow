"""FastAPI application: startup safety check, health and Kite login routes.
The control API, engine wiring and scheduler arrive in later phases."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import factory
from app.api import auth
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
    log.info("tradeflow starting", extra={"mode": settings.mode, "feed": settings.feed})
    yield
    log.info("tradeflow stopped")


app = FastAPI(title="tradeflow", lifespan=lifespan)
app.include_router(auth.router)


@app.get("/health")
async def health() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "mode": settings.mode, "time": now_ist().isoformat()}
