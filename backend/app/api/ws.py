"""WS /ws?token=... streams the status snapshot every second.

Browsers can't set headers on a WebSocket, so the admin token comes as a query
parameter (constant-time compared). The access-log filter in main.py redacts it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api import views
from app.config import MIN_ADMIN_TOKEN_LEN, get_settings

log = logging.getLogger(__name__)

router = APIRouter()

POLICY_VIOLATION = 1008
INTERVAL_SEC = 1.0


def _token_ok(token: str) -> bool:
    expected = get_settings().admin_token.get_secret_value()
    return len(expected) >= MIN_ADMIN_TOKEN_LEN and hmac.compare_digest(
        token.encode(), expected.encode()
    )


@router.websocket("/ws")
async def status_stream(ws: WebSocket, token: str = "") -> None:
    if not _token_ok(token):
        await ws.close(code=POLICY_VIOLATION, reason="invalid admin token")
        return
    runtime = getattr(ws.app.state, "runtime", None)
    await ws.accept()
    try:
        while True:
            runtime = getattr(ws.app.state, "runtime", None)
            if runtime is None:
                await ws.send_json({"type": "error", "detail": "runtime not started"})
            else:
                snap = views.status(runtime)
                await ws.send_text('{"type":"status","data":' + snap.model_dump_json() + "}")
            await asyncio.sleep(INTERVAL_SEC)
    except (WebSocketDisconnect, RuntimeError):
        return
