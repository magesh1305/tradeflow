"""FastAPI application. Phase 1: health endpoint and startup safety check only;
the control API, engine wiring and scheduler arrive in later phases."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.core.clock import now_ist
from app.core.logging import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    settings.assert_safe()  # refuses to start live unless every latch is set
    log.info("tradeflow starting", extra={"mode": settings.mode, "feed": settings.feed})
    yield
    log.info("tradeflow stopped")


app = FastAPI(title="tradeflow", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "mode": settings.mode, "time": now_ist().isoformat()}
