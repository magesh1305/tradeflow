"""Daily trading plan (the dashboard's Settings tab). Admin only.

GET  /settings          today's plan (or the pre-filled form) + strategy list
POST /settings/plan     submit / update today's plan; loosening during market hours
                        needs {"confirm": "LOOSEN"} (409 lists what would loosen)
GET  /settings/history  audit trail of today's submissions and changes
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import RuntimeDep, require_admin
from app.risk.plan import LOOSEN_CONFIRMATION, DailyPlan
from app.runtime import PlanError, PlanLooseningError

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_admin)])


class StrategyInfo(BaseModel):
    id: str
    strategy: str
    state: str
    has_lots: bool


class SettingsOut(BaseModel):
    trading_date: date
    submitted: bool
    submitted_at: datetime | None
    market_hours: bool
    loosen_confirmation: str
    plan: DailyPlan
    strategies: list[StrategyInfo]


class PlanIn(BaseModel):
    plan: DailyPlan
    confirm: str | None = None


class PlanResult(BaseModel):
    plan: DailyPlan
    changes: list[str]
    loosened: list[str]
    started: list[str]
    stopped: list[str]
    warnings: list[str]


class PlanEventOut(BaseModel):
    ts: datetime
    changes: list[str]
    loosened: list[str]


def _strategies(runtime: Any) -> list[StrategyInfo]:
    engine = runtime.engine
    if engine is None:
        return [
            StrategyInfo(id=c.id, strategy=c.strategy, state="STOPPED", has_lots=True)
            for c in runtime._configs()
        ]
    return [
        StrategyInfo(
            id=sid,
            strategy=slot.strategy.name,
            state=slot.state.value,
            has_lots="lots" in type(slot.strategy.params).model_fields,
        )
        for sid, slot in engine.slots.items()
    ]


@router.get("", response_model=SettingsOut)
async def get_settings_plan(runtime: RuntimeDep) -> SettingsOut:
    plan, submitted = await runtime.plan_for_form()
    return SettingsOut(
        trading_date=runtime.clock.now().date(),
        submitted=submitted,
        submitted_at=runtime.plan_submitted_at if submitted else None,
        market_hours=runtime.market_hours_now(),
        loosen_confirmation=LOOSEN_CONFIRMATION,
        plan=plan,
        strategies=_strategies(runtime),
    )


@router.post("/plan", response_model=PlanResult)
async def submit_plan(body: PlanIn, runtime: RuntimeDep) -> PlanResult:
    try:
        result = await runtime.submit_plan(body.plan, body.confirm)
    except PlanLooseningError as exc:
        raise HTTPException(409, {"message": str(exc), "loosened": exc.loosened}) from exc
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from exc
    return PlanResult(plan=body.plan, **result)


@router.get("/history", response_model=list[PlanEventOut])
async def plan_history(runtime: RuntimeDep, day: date | None = None) -> list[PlanEventOut]:
    try:
        rows = await runtime.plan_history(day)
    except Exception as exc:
        raise HTTPException(503, f"plan history unavailable: {exc}") from exc
    return [PlanEventOut(ts=r["ts"], changes=r["changes"], loosened=r["loosened"]) for r in rows]
