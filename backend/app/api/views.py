"""Engine state -> API models. Shared by REST endpoints and the /ws status stream."""

from __future__ import annotations

from typing import Any

from app.api.schemas import (
    ChainRowOut,
    FeedOut,
    FillOut,
    GreeksOut,
    KillSwitchOut,
    OptionQuoteOut,
    OrderOut,
    Pnl,
    PositionOut,
    StatusOut,
    StrategyDetail,
    StrategyOut,
)
from app.core.models import Fill, Order
from app.data.option_chain import ChainRow, OptionQuote
from app.engine.engine import StrategySlot, TradingEngine
from app.engine.positions import PnlSummary, Position
from app.runtime import Runtime


def pnl(p: PnlSummary) -> Pnl:
    return Pnl(
        gross=round(p.gross, 2),
        net=round(p.net, 2),
        realized=round(p.realized, 2),
        unrealized=round(p.unrealized, 2),
        charges=round(p.charges, 2),
    )


def position(p: Position, stop: float | None = None) -> PositionOut:
    return PositionOut(
        strategy_id=p.strategy_id,
        exchange=str(p.instrument.exchange),
        tradingsymbol=p.instrument.tradingsymbol,
        instrument_token=p.instrument.token,
        product=str(p.product),
        quantity=p.quantity,
        average_price=round(p.average_price, 4),
        last_price=p.last_price,
        pnl=Pnl(
            gross=round(p.gross_pnl, 2),
            net=round(p.net_pnl, 2),
            realized=round(p.realized_pnl, 2),
            unrealized=round(p.unrealized_pnl, 2),
            charges=round(p.charges, 2),
        ),
        protected_stop=stop,
    )


def order(o: Order, mode: str) -> OrderOut:
    r = o.request
    return OrderOut(
        client_order_id=o.client_order_id,
        broker_order_id=o.broker_order_id,
        mode=mode,
        strategy_id=r.strategy_id,
        exchange=str(r.instrument.exchange),
        tradingsymbol=r.instrument.tradingsymbol,
        side=str(r.side),
        quantity=r.quantity,
        order_type=str(r.order_type),
        product=str(r.product),
        price=r.price,
        trigger_price=r.trigger_price,
        status=str(o.status),
        filled_quantity=o.filled_quantity,
        average_price=o.average_price,
        status_message=o.status_message,
        reason=r.reason,
        leg_group_id=r.leg_group_id,
        protective=r.protective,
        created_at=o.created_at,
        updated_at=o.updated_at,
    )


def fill(f: Fill, mode: str, charges: float) -> FillOut:
    return FillOut(
        fill_id=f.fill_id,
        client_order_id=f.client_order_id,
        broker_order_id=f.broker_order_id,
        mode=mode,
        strategy_id=f.strategy_id,
        exchange=str(f.instrument.exchange),
        tradingsymbol=f.instrument.tradingsymbol,
        side=str(f.side),
        quantity=f.quantity,
        price=f.price,
        product=str(f.product),
        charges=round(charges, 4),
        ts=f.ts,
        trading_date=f.ts.date(),
    )


def strategy(engine: TradingEngine, slot: StrategySlot) -> StrategyOut:
    sid = slot.strategy.id
    return StrategyOut(
        id=sid,
        strategy=slot.strategy.name,
        state=slot.state.value,
        error=slot.error,
        pnl=pnl(engine.positions.pnl(sid)),
        open_positions=len(engine.positions.open_positions(sid)),
        open_orders=sum(1 for o in engine.open_orders() if o.request.strategy_id == sid),
    )


def strategy_detail(engine: TradingEngine, slot: StrategySlot) -> StrategyDetail:
    base = strategy(engine, slot)
    stops = engine.stops.protected()
    return StrategyDetail(
        **base.model_dump(),
        params=slot.strategy.params.model_dump(mode="json"),
        params_schema=slot.strategy.Params.model_json_schema(),
        positions=[
            position(p, stops.get(p.key))
            for p in engine.positions.positions(slot.strategy.id)
            if p.quantity or p.realized_pnl
        ],
        subscribed_tokens=len(slot.tick_tokens) + len(slot.candle_subs),
    )


def status(runtime: Runtime) -> StatusOut:
    s = runtime.settings
    e = runtime.engine
    ks = e.risk.kill_switch if e else None
    empty = Pnl(gross=0, net=0, realized=0, unrealized=0, charges=0)
    stops = e.stops.protected() if e else {}
    return StatusOut(
        time=runtime.clock.now(),
        mode=s.mode,
        engine_running=bool(e and e.running),
        blocked_reason=e.blocked_reason if e else "",
        feed=FeedOut(
            source=s.feed,
            connected=bool(e and e.feed_connected),
            detail=e.feed_detail if e else "engine not started",
        ),
        kill_switch=KillSwitchOut(
            tripped=bool(ks and ks.tripped),
            reason=ks.reason if ks else "",
            tripped_at=ks.tripped_at if ks else None,
        ),
        pnl=pnl(e.positions.pnl()) if e else empty,
        positions=[position(p, stops.get(p.key)) for p in e.positions.open_positions()]
        if e
        else [],
        open_orders=[order(o, s.mode) for o in e.open_orders()] if e else [],
        strategies=[strategy(e, slot) for slot in e.slots.values()] if e else [],
        open_leg_groups=len(e.legs.open_groups()) if e else 0,
        journal={
            "backlog": runtime.journal.backlog,
            "failures": runtime.journal.failures,
            "last_error": runtime.journal.last_error,
        },
    )


def _quote(q: OptionQuote | None) -> OptionQuoteOut | None:
    if q is None:
        return None
    g = q.greeks
    return OptionQuoteOut(
        tradingsymbol=q.instrument.tradingsymbol,
        instrument_token=q.instrument.token,
        ltp=q.ltp,
        bid=q.bid,
        ask=q.ask,
        oi=q.oi,
        volume=q.volume,
        greeks=GreeksOut(
            iv=round(g.iv, 4),
            delta=round(g.delta, 4),
            gamma=round(g.gamma, 6),
            theta=round(g.theta, 2),
            vega=round(g.vega, 2),
        )
        if g
        else None,
    )


def chain_row(r: ChainRow) -> ChainRowOut:
    return ChainRowOut(strike=r.strike, is_atm=r.is_atm, call=_quote(r.call), put=_quote(r.put))


def db_order(row: dict[str, Any]) -> OrderOut:
    return OrderOut(
        **{k: row.get(k) for k in OrderOut.model_fields if k in row},
        protective=row.get("reason") == "protective stop",
    )


def db_fill(row: dict[str, Any]) -> FillOut:
    return FillOut(**{k: row[k] for k in FillOut.model_fields if k in row})
