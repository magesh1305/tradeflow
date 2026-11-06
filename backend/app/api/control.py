"""Control API. Every route here requires the X-Admin-Token header (router dependency).

Orders and fills combine the Postgres journal (history) with the live engine
(today, including anything the journal writer hasn't flushed yet).
"""

from __future__ import annotations

import contextlib
import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

from app.api import views
from app.api.deps import RuntimeDep, require_admin
from app.api.schemas import (
    BacktestDetail,
    BacktestIn,
    BacktestRunOut,
    DailyPnlOut,
    FillOut,
    KillSwitchIn,
    KillSwitchOut,
    OptionChainOut,
    OrderOut,
    ParamsIn,
    RiskOut,
    StatusOut,
    StrategyDetail,
    StrategyOut,
)
from app.backtest.runner import BacktestRequest
from app.data.historical import INTERVAL_SECONDS
from app.data.instruments import InstrumentNotFoundError
from app.engine.engine import EngineBlockedError, StrategyRunningError, TradingEngine
from app.runtime import Runtime
from app.storage.journal import ReportFilter
from app.strategies.registry import StrategyConfig, StrategyConfigError, build, discover

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])

Limit = Annotated[int, Query(ge=1, le=5000)]


def _engine(runtime: Runtime) -> TradingEngine:
    if runtime.engine is None:
        raise HTTPException(503, "engine not running (log in to Kite or check the logs)")
    return runtime.engine


def _slot(engine: TradingEngine, strategy_id: str) -> Any:
    slot = engine.slots.get(strategy_id)
    if slot is None:
        raise HTTPException(404, f"unknown strategy {strategy_id!r}")
    return slot


def _filter(
    runtime: Runtime,
    start: date | None,
    end: date | None,
    strategy: str | None,
    mode: str | None,
    limit: int,
) -> ReportFilter:
    today = runtime.clock.now().date()
    f = ReportFilter(start or today, end or today, mode or runtime.settings.mode, strategy, limit)
    if f.end < f.start:
        raise HTTPException(422, "`to` is before `from`")
    return f


# --- status -------------------------------------------------------------------------------


@router.get("/status", response_model=StatusOut)
async def get_status(runtime: RuntimeDep) -> StatusOut:
    return views.status(runtime)


# --- strategies ---------------------------------------------------------------------------


@router.get("/strategies", response_model=list[StrategyOut])
async def list_strategies(runtime: RuntimeDep) -> list[StrategyOut]:
    e = _engine(runtime)
    return [views.strategy(e, slot) for slot in e.slots.values()]


@router.get("/strategies/{strategy_id}", response_model=StrategyDetail)
async def get_strategy(strategy_id: str, runtime: RuntimeDep) -> StrategyDetail:
    e = _engine(runtime)
    return views.strategy_detail(e, _slot(e, strategy_id))


@router.post("/strategies/{strategy_id}/start", response_model=StrategyDetail)
async def start_strategy(strategy_id: str, runtime: RuntimeDep) -> StrategyDetail:
    e = _engine(runtime)
    slot = _slot(e, strategy_id)
    try:
        e.start_strategy(strategy_id)
    except EngineBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:  # kill switch active
        raise HTTPException(409, str(exc)) from exc
    log.warning("strategy started via API", extra={"strategy": strategy_id})
    return views.strategy_detail(e, slot)


@router.post("/strategies/{strategy_id}/stop", response_model=StrategyDetail)
async def stop_strategy(
    strategy_id: str, runtime: RuntimeDep, exit_positions: bool = False
) -> StrategyDetail:
    e = _engine(runtime)
    slot = _slot(e, strategy_id)
    e.stop_strategy(strategy_id, exit_positions=exit_positions)
    log.warning(
        "strategy stopped via API",
        extra={"strategy": strategy_id, "exit_positions": exit_positions},
    )
    return views.strategy_detail(e, slot)


@router.put("/strategies/{strategy_id}/params", response_model=StrategyDetail)
async def put_params(strategy_id: str, body: ParamsIn, runtime: RuntimeDep) -> StrategyDetail:
    e = _engine(runtime)
    slot = _slot(e, strategy_id)
    try:
        runtime.set_strategy_params(strategy_id, body.params)
    except StrategyRunningError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc
    return views.strategy_detail(e, slot)


# --- orders, fills, P&L ---------------------------------------------------------------------------


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    runtime: RuntimeDep,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    strategy: str | None = None,
    mode: str | None = None,
    limit: Limit = 1000,
) -> list[OrderOut]:
    f = _filter(runtime, start, end, strategy, mode, limit)
    rows: dict[str, OrderOut] = {}
    try:
        for r in await runtime.journal.store.query_orders(f):
            rows[r["client_order_id"]] = views.db_order(r)
    except Exception:
        log.exception("order history query failed; showing live orders only")
    e = runtime.engine
    if e is not None and f.mode == runtime.settings.mode:
        for o in e.orders.values():
            d = o.created_at.date()
            if f.start <= d <= f.end and (f.strategy_id in (None, o.request.strategy_id)):
                rows[o.client_order_id] = views.order(o, f.mode)  # live state wins
    out = sorted(rows.values(), key=lambda o: o.created_at, reverse=True)
    return out[:limit]


@router.get("/fills", response_model=list[FillOut])
async def list_fills(
    runtime: RuntimeDep,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    strategy: str | None = None,
    mode: str | None = None,
    limit: Limit = 1000,
) -> list[FillOut]:
    f = _filter(runtime, start, end, strategy, mode, limit)
    rows: dict[str, FillOut] = {}
    try:
        for r in await runtime.journal.store.query_fills(f):
            rows[r["fill_id"]] = views.db_fill(r)
    except Exception:
        log.exception("fill history query failed; showing live fills only")
    e = runtime.engine
    if e is not None and f.mode == runtime.settings.mode:
        for fl in e.fills:
            if f.start <= fl.ts.date() <= f.end and f.strategy_id in (None, fl.strategy_id):
                rows[fl.fill_id] = views.fill(fl, f.mode, e.fill_charges.get(fl.fill_id, 0.0))
    return sorted(rows.values(), key=lambda x: x.ts, reverse=True)[:limit]


@router.get("/pnl/daily", response_model=list[DailyPnlOut])
async def daily_pnl(
    runtime: RuntimeDep,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    strategy: str | None = None,
    mode: str | None = None,
) -> list[DailyPnlOut]:
    today = runtime.clock.now().date()
    f = _filter(runtime, start or date(today.year - 1, today.month, 1), end, strategy, mode, 5000)
    rows: dict[tuple[date, str], DailyPnlOut] = {}
    try:
        for r in await runtime.journal.store.query_daily_pnl(f):
            rows[(r["trading_date"], r["strategy_id"])] = DailyPnlOut(
                **{k: r[k] for k in DailyPnlOut.model_fields if k in r}
            )
    except Exception:
        log.exception("daily P&L query failed")
    e = runtime.engine
    if e is not None and f.start <= today <= f.end and f.mode == runtime.settings.mode:
        for sid, v in e.daily_summary().items():
            if f.strategy_id in (None, sid):
                rows[(today, sid)] = DailyPnlOut(
                    trading_date=today,
                    mode=f.mode,
                    strategy_id=sid,
                    gross_pnl=round(v["gross"], 2),
                    charges=round(v["charges"], 2),
                    net_pnl=round(v["net"], 2),
                    trades=int(v["trades"]),
                    live=True,
                )
    return sorted(rows.values(), key=lambda r: (r.trading_date, r.strategy_id))


# --- risk ---------------------------------------------------------------------------------


@router.get("/risk", response_model=RiskOut)
async def risk(runtime: RuntimeDep) -> RiskOut:
    e = _engine(runtime)
    lim = e.risk.limits
    pnl = e.positions.pnl()
    ks = e.risk.kill_switch
    margin = None
    if e.margin is not None:
        margin = await e.margin.funds(e.positions.open_positions(), e.open_orders())
    limits = {
        "max_lots_per_order": lim.max_lots_per_order,
        "max_open_positions": lim.max_open_positions,
        "max_orders_per_minute": lim.max_orders_per_minute,
        "daily_loss_limit": lim.daily_loss_limit,
        "duplicate_window_sec": lim.duplicate_window_sec,
        "trading_start": lim.trading_start.isoformat("minutes"),
        "trading_end": lim.trading_end.isoformat("minutes"),
        "square_off_time": e.config.square_off_time.isoformat("minutes"),
        "expiry_no_entry_after": lim.expiry_no_entry_after.isoformat("minutes"),
        "option_max_spread_pct": lim.option_max_spread_pct,
        "option_min_volume": lim.option_min_volume,
        "freeze_limits": lim.freeze_limits,
    }
    loss_used = max(-pnl.net, 0.0)
    usage = {
        "orders_last_minute": e.risk.orders_last_minute(),
        "open_positions": len(
            {(p.instrument.token, p.product) for p in e.positions.open_positions()}
        ),
        "net_pnl": round(pnl.net, 2),
        "daily_loss_used": round(loss_used, 2),
        "daily_loss_used_pct": round(loss_used / lim.daily_loss_limit * 100, 2),
        "rejections_today": e.risk.rejections,
        "open_orders": len(e.open_orders()),
    }
    return RiskOut(
        limits=limits,
        usage=usage,
        kill_switch=KillSwitchOut(tripped=ks.tripped, reason=ks.reason, tripped_at=ks.tripped_at),
        margin=margin,
    )


# --- option chain -------------------------------------------------------------------------


@router.get("/option-chain/{underlying}", response_model=OptionChainOut)
async def option_chain(
    underlying: str,
    runtime: RuntimeDep,
    expiry: date | None = None,
    width: Annotated[int, Query(ge=1, le=40)] = 10,
) -> OptionChainOut:
    e = _engine(runtime)
    underlying = underlying.upper()
    today = e.clock.now().date()
    expiries = [d for d in e.instruments.expiries(underlying) if d >= today]
    if not expiries:
        raise HTTPException(404, f"no option expiries for {underlying}")
    exp = expiry or expiries[0]
    if exp not in expiries:
        raise HTTPException(422, f"{exp} is not a listed expiry for {underlying}")
    with contextlib.suppress(InstrumentNotFoundError):
        e.subscribe_market([e.instruments.spot(underlying).token])
    spot = e.option_chain.spot(underlying)
    if spot is None:
        return OptionChainOut(
            underlying=underlying,
            expiry=exp,
            expiries=expiries,
            spot=None,
            rows=[],
            note="waiting for the first spot price; retry shortly",
        )
    e.subscribe_market(e.option_chain.tokens_around_atm(underlying, exp, width))
    rows = e.option_chain.chain(underlying, exp, width)
    return OptionChainOut(
        underlying=underlying,
        expiry=exp,
        expiries=expiries,
        spot=spot,
        rows=[views.chain_row(r) for r in rows],
    )


# --- actions ------------------------------------------------------------------------------


@router.post("/square-off", response_model=StatusOut)
async def square_off(runtime: RuntimeDep) -> StatusOut:
    e = _engine(runtime)
    log.warning("manual square-off via API")
    e.square_off_all("manual square-off (dashboard)")
    return views.status(runtime)


@router.post("/kill-switch", response_model=KillSwitchOut)
async def kill_switch(body: KillSwitchIn, runtime: RuntimeDep) -> KillSwitchOut:
    e = _engine(runtime)
    log.critical("kill switch via API", extra={"reason": body.reason})
    e.trip_kill_switch(body.reason)
    ks = e.risk.kill_switch
    return KillSwitchOut(tripped=ks.tripped, reason=ks.reason, tripped_at=ks.tripped_at)


@router.post("/kill-switch/reset", response_model=KillSwitchOut)
async def kill_switch_reset(runtime: RuntimeDep) -> KillSwitchOut:
    e = _engine(runtime)
    e.reset_kill_switch()  # strategies stay stopped
    ks = e.risk.kill_switch
    return KillSwitchOut(tripped=ks.tripped, reason=ks.reason, tripped_at=ks.tripped_at)


# --- backtests ----------------------------------------------------------------------------


@router.post("/backtests", response_model=BacktestRunOut, status_code=202)
async def create_backtest(body: BacktestIn, runtime: RuntimeDep) -> BacktestRunOut:
    if body.end < body.start:
        raise HTTPException(422, "end is before start")
    discover()
    try:
        build(StrategyConfig(id="validate", strategy=body.strategy, params=body.params))
    except StrategyConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    req = BacktestRequest(
        body.strategy, body.params, body.start, body.end, INTERVAL_SECONDS[body.interval]
    )
    try:
        run_id = await runtime.submit_backtest(req)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    row = await runtime.backtests.get(run_id) if runtime.backtests else None
    if row is None:
        raise HTTPException(500, "backtest run not recorded")
    return BacktestRunOut(**row)


@router.get("/backtests", response_model=list[BacktestRunOut])
async def list_backtests(
    runtime: RuntimeDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[BacktestRunOut]:
    if runtime.backtests is None:
        return []
    rows = await runtime.backtests.list(limit)
    out = []
    for row in rows:
        m = row.get("metrics")
        # The list view doesn't carry per-trade detail.
        slim = {**row, "metrics": {k: v for k, v in m.items() if k != "trades_list"}} if m else row
        out.append(BacktestRunOut(**slim))
    return out


@router.get("/backtests/{run_id}", response_model=BacktestDetail)
async def get_backtest(run_id: int, runtime: RuntimeDep) -> BacktestDetail:
    row = await runtime.backtests.get(run_id) if runtime.backtests else None
    if row is None:
        raise HTTPException(404, "no such backtest")
    return BacktestDetail(**row)
