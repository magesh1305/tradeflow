"""Connection check (admin): what is configured, what is reachable, and what live
mode still needs. Read-only by design: Kite keys and the live-mode switches live in
the server's .env and can never be changed from the dashboard.

GET  /connection        configuration and reachability report (never returns secrets)
POST /connection/kite   test the Kite session with a read-only profile call
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from app import factory
from app.api.deps import RuntimeDep, require_admin
from app.brokers.kite import BrokerError, SessionExpiredError
from app.brokers.session import KiteAuth
from app.config import MIN_ADMIN_TOKEN_LEN, Settings, get_settings
from app.storage.db import make_engine

log = logging.getLogger(__name__)

router = APIRouter(prefix="/connection", tags=["connection"], dependencies=[Depends(require_admin)])


class Check(BaseModel):
    label: str
    ok: bool | None  # None = can't be checked from here
    fix: str = ""


class KiteInfo(BaseModel):
    api_key_set: bool
    api_secret_set: bool
    user_id_set: bool
    redirect_url: str
    logged_in: bool
    user_id: str = ""
    expires_at: str = ""
    session_error: str = ""


class DatabaseInfo(BaseModel):
    used: bool
    ok: bool | None
    detail: str = ""


class ConnectionOut(BaseModel):
    mode: str
    feed: str
    storage: str
    env_file: str
    kite: KiteInfo
    database: DatabaseInfo
    live_ready: bool
    live_checks: list[Check]


class KiteTestOut(BaseModel):
    ok: bool
    detail: str
    user_id: str = ""
    user_name: str = ""
    broker: str = ""


async def _database(s: Settings) -> DatabaseInfo:
    if s.storage != "postgres":
        return DatabaseInfo(used=False, ok=None, detail="not used (practice storage in memory)")
    engine = make_engine(s.database_url)
    try:
        async with asyncio.timeout(4):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return DatabaseInfo(used=True, ok=True, detail="connected")
    except Exception as exc:
        return DatabaseInfo(used=True, ok=False, detail=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        await engine.dispose()


async def _kite(request: Request, s: Settings) -> KiteInfo:
    info = KiteInfo(
        api_key_set=bool(s.kite_api_key),
        api_secret_set=bool(s.kite_api_secret.get_secret_value()),
        user_id_set=bool(s.kite_user_id),
        redirect_url=f"{s.frontend_origin.rstrip('/')}/api/auth/callback",
        logged_in=False,
    )
    auth: KiteAuth | None = getattr(request.app.state, "auth", None)
    if auth is None:
        return info
    try:
        session = await auth.current_session()
    except Exception as exc:
        info.session_error = f"login store unreachable (Redis): {type(exc).__name__}"
        return info
    if session is not None:
        info.logged_in = True
        info.user_id = session.user_id
        info.expires_at = session.expires_at.isoformat()
    return info


def _live_checks(s: Settings, kite: KiteInfo, db: DatabaseInfo) -> list[Check]:
    return [
        Check(label="MODE=live", ok=s.mode == "live", fix="Set MODE=live in the server's .env"),
        Check(
            label="LIVE_TRADING_CONFIRMED=true",
            ok=s.live_trading_confirmed,
            fix="Set LIVE_TRADING_CONFIRMED=true in the server's .env",
        ),
        Check(
            label=f"ADMIN_TOKEN has {MIN_ADMIN_TOKEN_LEN}+ characters",
            ok=len(s.admin_token.get_secret_value()) >= MIN_ADMIN_TOKEN_LEN,
            fix="Use a long random ADMIN_TOKEN",
        ),
        Check(label="FEED=kite (real prices)", ok=s.feed == "kite", fix="Set FEED=kite"),
        Check(
            label="Kite API key and secret",
            ok=kite.api_key_set and kite.api_secret_set,
            fix="Set KITE_API_KEY and KITE_API_SECRET from developers.kite.trade",
        ),
        Check(
            label="KITE_USER_ID (recommended)",
            ok=kite.user_id_set,
            fix="Set KITE_USER_ID so only your Zerodha account can log in",
        ),
        Check(label="STORAGE=postgres", ok=s.storage == "postgres", fix="Set STORAGE=postgres"),
        Check(
            label="Database reachable",
            ok=db.ok if db.used else False,
            fix="Start the database (docker compose up) and check DATABASE_URL",
        ),
        Check(
            label="Logged in to Zerodha today",
            ok=kite.logged_in,
            fix="Press 'Log in to Kite' on Home (needed every morning)",
        ),
        Check(
            label="Server's static IP whitelisted in the Kite console",
            ok=None,
            fix="Required by Zerodha for placing orders; can't be checked without sending one",
        ),
    ]


@router.get("", response_model=ConnectionOut)
async def connection(request: Request) -> ConnectionOut:
    s = get_settings()
    kite = await _kite(request, s)
    db = await _database(s)
    return ConnectionOut(
        mode=s.mode,
        feed=s.feed,
        storage=s.storage,
        env_file="backend/.env (development) or .env next to docker-compose.yml (Docker)",
        kite=kite,
        database=db,
        live_ready=not s.live_latch_failures() and kite.logged_in and db.ok is True,
        live_checks=_live_checks(s, kite, db),
    )


@router.post("/kite", response_model=KiteTestOut)
async def test_kite(request: Request, runtime: RuntimeDep) -> KiteTestOut:
    s = get_settings()
    auth: KiteAuth | None = getattr(request.app.state, "auth", None)
    if auth is None:
        raise HTTPException(409, "Kite API key and secret are not set in the server's .env")
    try:
        session = await auth.current_session()
    except Exception as exc:
        raise HTTPException(503, "The login store (Redis) is not reachable") from exc
    if session is None:
        raise HTTPException(409, "Log in to Zerodha first (Home > Log in to Kite)")
    broker = runtime.broker or factory.build_broker(s, session)
    try:
        profile: dict[str, Any] = await broker.profile()
    except SessionExpiredError:
        return KiteTestOut(ok=False, detail="The Zerodha session has expired. Log in again.")
    except BrokerError as exc:
        return KiteTestOut(ok=False, detail=f"Zerodha answered with an error: {exc}")
    return KiteTestOut(
        ok=True,
        detail="Connected to Zerodha. Market data works; no order was placed.",
        user_id=str(profile.get("user_id", "")),
        user_name=str(profile.get("user_name", "")),
        broker=str(profile.get("broker", "")),
    )
