"""Crash-recovery status and operator resolution (admin only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import Admin, RuntimeDep

router = APIRouter(prefix="/recovery", tags=["recovery"])


class ResolveRequest(BaseModel):
    # True: book the differences to strategy "__recovered__" so the system's
    # positions match Kite (they can then be squared off from the dashboard).
    # False: the operator has fixed things at the broker; just lift the block.
    adopt_broker_positions: bool = False
    confirm: str

    def confirmed(self) -> bool:
        return self.confirm == "RESOLVE"


@router.get("")
async def recovery_status(_: Admin, runtime: RuntimeDep) -> dict[str, Any]:
    return runtime.status()


@router.post("/resolve")
async def resolve(body: ResolveRequest, _: Admin, runtime: RuntimeDep) -> dict[str, Any]:
    if not body.confirmed():
        raise HTTPException(400, 'type "RESOLVE" in `confirm` to lift the recovery block')
    if runtime.engine is None:
        raise HTTPException(409, "engine not running")
    if not runtime.engine.blocked_reason:
        raise HTTPException(409, "engine is not blocked")
    return await runtime.resolve_recovery(body.adopt_broker_positions)
