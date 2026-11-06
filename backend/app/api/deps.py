"""Shared API dependencies."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from app.config import MIN_ADMIN_TOKEN_LEN, get_settings
from app.runtime import Runtime


def require_admin(x_admin_token: Annotated[str, Header()] = "") -> None:
    """Constant-time check of the X-Admin-Token header."""
    expected = get_settings().admin_token.get_secret_value()
    if len(expected) < MIN_ADMIN_TOKEN_LEN:
        raise HTTPException(503, "ADMIN_TOKEN is not configured (16+ characters required)")
    if not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(401, "invalid admin token")


Admin = Annotated[None, Depends(require_admin)]


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(503, "runtime not started")
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
