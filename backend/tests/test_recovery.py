from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from kiteconnect import KiteConnect

from app.brokers.kite import KiteBroker
from app.core.clock import ManualClock
from app.core.models import Product, Side
from app.data.instruments import InstrumentStore
from app.engine.engine import EngineBlockedError, StrategyState, TradingEngine
from app.engine.positions import PositionBook
from app.engine.recovery import (
    RECOVERED,
    adopt_broker_positions,
    compare_with_broker,
    rebuild,
)
from app.execution.paper import PaperExecutor
from app.notify.base import LogNotifier
from app.risk.manager import RiskLimits, RiskManager
from app.storage.journal import Journal, MemoryJournalStore
from tests.builders import CE, FUT, ist, tick
from tests.test_engine import ManualFeed, Recorder, push
from tests.test_live_executor import FastLimiter

TODAY = date(2025, 11, 3)


async def new_engine(journal: Journal | None = None, h: int = 10) -> TradingEngine:
    clock = ManualClock(ist(h, 0))
    store = InstrumentStore()
    store.load([FUT, CE], TODAY)
    positions = PositionBook()
    engine = TradingEngine(
        instruments=store,
        feed=ManualFeed(),
        executor=PaperExecutor(clock, 0),
        risk=RiskManager(RiskLimits(), positions, clock),
        positions=positions,
        clock=clock,
        notifier=LogNotifier(),
        journal=journal,
    )
    engine.add_strategy(Recorder("s"))
    return engine


async def trade_then_crash(store: MemoryJournalStore) -> None:
    """A first process trades, journals, then dies."""
    j = Journal(store, "paper", LogNotifier())
    await j.start()
    engine = await new_engine(j)
    await engine.start(["s"])
    await push(
        engine,
        tick(FUT, 100, ist(10, 0)),
        tick(CE, 50, ist(10, 0), bid=49.9, ask=50.1, volume=100_000),
    )
    s = engine.slots["s"].strategy
    s.buy(FUT, 150)
    s.sell(FUT, 75)
    s.sell(CE, 75)
    from app.core.models import OrderType  # noqa: PLC0415

    s.buy(CE, 75, order_type=OrderType.LIMIT, price=10.0)  # resting when we "crash"
    engine._pump()
    await j.flush()
    await j.stop()


async def test_rebuild_restores_positions_and_open_orders() -> None:
    store = MemoryJournalStore()
    await trade_then_crash(store)
    engine = await new_engine()
    report = await rebuild(engine, store, "paper", TODAY)
    assert report.fills_replayed == 3
    assert engine.positions.net_quantity("s", FUT.token) == 75
    assert engine.positions.net_quantity("s", CE.token) == -75
    pos = engine.positions.get("s", FUT.token, Product.MIS)
    assert pos is not None and pos.average_price == pytest.approx(100.0)
    assert report.orders_restored == 1
    (restored,) = [o for o in engine.orders.values() if o.request.price == 10.0]
    assert restored.status.value == "CANCELLED"  # paper orders don't survive restarts


async def test_carried_nrml_positions_without_old_pnl() -> None:
    store = MemoryJournalStore()
    yesterday = date(2025, 10, 31)
    store.orders["C" * 16] = {
        "client_order_id": "C" * 16,
        "mode": "paper",
        "trading_date": yesterday,
        "status": "COMPLETE",
    }
    base: dict[str, Any] = {
        "client_order_id": "C" * 16,
        "broker_order_id": None,
        "mode": "paper",
        "strategy_id": "s",
        "exchange": "NFO",
        "charges": 0.0,
        "product": "NRML",
        "trading_date": yesterday,
    }
    store.fills["F1"] = {
        **base,
        "fill_id": "F1",
        "tradingsymbol": FUT.tradingsymbol,
        "instrument_token": FUT.token,
        "side": "BUY",
        "quantity": 150,
        "price": 100.0,
        "ts": ist(10, 0, d=yesterday),
    }
    store.fills["F2"] = {
        **base,
        "fill_id": "F2",
        "tradingsymbol": FUT.tradingsymbol,
        "instrument_token": FUT.token,
        "side": "SELL",
        "quantity": 75,
        "price": 110.0,
        "ts": ist(11, 0, d=yesterday),
    }
    store.fills["F3"] = {
        **base,
        "fill_id": "F3",
        "tradingsymbol": "NIFTYOLDFUT",
        "instrument_token": 7,
        "side": "BUY",
        "quantity": 75,
        "price": 1.0,
        "ts": ist(11, 0, d=yesterday),
    }  # expired contract
    engine = await new_engine()
    report = await rebuild(engine, store, "paper", TODAY)
    assert report.carried_positions == 1
    pos = engine.positions.get("s", FUT.token, Product.NRML)
    assert pos is not None and pos.quantity == 75
    assert pos.realized_pnl == 0  # yesterday's P&L is not today's


def broker_with(positions: list[dict[str, Any]], orders: list[dict[str, Any]]) -> KiteBroker:
    kite = MagicMock(spec=KiteConnect)
    kite.positions.return_value = {"net": positions, "day": []}
    kite.orders.return_value = orders
    return KiteBroker(
        kite, orders_enabled=True, order_limiter=FastLimiter(), data_limiter=FastLimiter()
    )


def kite_pos(inst: Any, qty: int, product: str = "MIS", avg: float = 100.0) -> dict[str, Any]:
    return {
        "exchange": "NFO",
        "tradingsymbol": inst.tradingsymbol,
        "instrument_token": inst.token,
        "product": product,
        "quantity": qty,
        "average_price": avg,
    }


async def test_broker_comparison_matches() -> None:
    store = MemoryJournalStore()
    await trade_then_crash(store)
    engine = await new_engine()
    report = await rebuild(engine, store, "paper", TODAY)
    known_tag = next(iter(store.orders))
    broker = broker_with(
        [
            kite_pos(FUT, 75),
            kite_pos(CE, -75),
            {
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "instrument_token": 1,
                "product": "CNC",
                "quantity": 10,
            },
        ],  # equity holdings are not ours to compare
        [
            {"order_id": "1", "tag": known_tag, "status": "COMPLETE"},
            {"order_id": "2", "tag": None, "status": "COMPLETE"},
        ],  # manual order
    )
    await compare_with_broker(engine, broker, store, "paper", TODAY, report=report)
    assert report.ok, report.mismatches
    assert report.checked_with_broker


async def test_broker_mismatch_blocks_until_resolved_with_adoption() -> None:
    store = MemoryJournalStore()
    await trade_then_crash(store)
    engine = await new_engine()
    report = await rebuild(engine, store, "paper", TODAY)
    broker = broker_with(
        [kite_pos(FUT, 150, avg=101.0), kite_pos(CE, -75)],
        [{"order_id": "9", "tag": "ZZZZZZZZZZZZZZZZ", "status": "OPEN"}],
    )
    await compare_with_broker(engine, broker, store, "paper", TODAY, report=report)
    assert not report.ok
    assert any("ours=75 kite=150" in m for m in report.mismatches)
    assert any("missing from the journal" in m for m in report.mismatches)

    engine.block("; ".join(report.mismatches))
    await engine.start(["s"])  # requested, but blocked
    assert engine.slots["s"].state is StrategyState.STOPPED
    with pytest.raises(EngineBlockedError):
        engine.start_strategy("s")

    adopted = adopt_broker_positions(engine, report.position_diffs, ist(10, 5))
    assert adopted == 1
    pos = engine.positions.get(RECOVERED, FUT.token, Product.MIS)
    assert pos is not None and (pos.quantity, pos.average_price) == (75, 101.0)
    engine.unblock()
    engine.start_strategy("s")
    assert engine.slots["s"].state is StrategyState.RUNNING
    # The adopted position is squared off like any other.
    engine.square_off_all("manual")
    await push(engine, tick(FUT, 102, ist(10, 6)))  # paper exit fills on the next price
    assert engine.positions.net_quantity(RECOVERED, FUT.token) == 0
    assert any(
        o.request.side is Side.SELL and o.request.strategy_id == RECOVERED
        for o in engine.orders.values()
    )
    await engine.stop()
