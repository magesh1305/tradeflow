"""Daily Kite login endpoints (public, no admin token, per the spec).

- GET /auth/login     -> 302 to Kite's login page
- GET /auth/callback  -> Kite redirects here with request_token; exchanged for an
                         access token, stored until the next 06:00 IST, then the
                         browser is sent back to the dashboard
- GET /auth/status    -> whether a valid Kite session exists
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.brokers.session import KiteAuth, LoginError
from app.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth(request: Request) -> KiteAuth:
    auth: KiteAuth | None = getattr(request.app.state, "auth", None)
    if auth is None:
        raise HTTPException(503, "Kite is not configured (set KITE_API_KEY / KITE_API_SECRET)")
    return auth


def _back_to_dashboard(**params: str) -> RedirectResponse:
    url = get_settings().frontend_url.rstrip("/") + "/?" + urlencode(params)
    return RedirectResponse(url, status_code=303)


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    return RedirectResponse(_auth(request).login_url(), status_code=302)


@router.get("/callback")
async def callback(
    request: Request, request_token: str = "", status: str = "success"
) -> RedirectResponse:
    auth = _auth(request)
    if status != "success":
        log.warning("kite login not successful", extra={"status": status})
        return _back_to_dashboard(kite_login="failed", reason=f"status={status}")
    try:
        session = await auth.complete_login(request_token)
    except LoginError as exc:
        log.error("kite login failed", extra={"error": str(exc)})
        return _back_to_dashboard(kite_login="failed", reason=str(exc)[:200])
    return _back_to_dashboard(kite_login="success", user_id=session.user_id)


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    auth: KiteAuth | None = getattr(request.app.state, "auth", None)
    if auth is None:
        return {"configured": False, "logged_in": False}
    try:
        session = await auth.current_session()
    except Exception:
        log.exception("could not read kite session")
        raise HTTPException(503, "session store unavailable") from None
    if session is None:
        return {"configured": True, "logged_in": False}
    return {
        "configured": True,
        "logged_in": True,
        "user_id": session.user_id,
        "login_time": session.login_time.isoformat(),
        "expires_at": session.expires_at.isoformat(),
    }
