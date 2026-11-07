"""A full simulated trading day in paper mode for both example strategies, wired
exactly as production does it (factory.build_engine). Each run must end flat."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any

from app import factory
from app.config import Settings
from app.core.clock import IST, ManualClock
from app.core.models import OrderStatus
from app.data.instruments import InstrumentStore
from app.engine.engine import StrategyState, TradingEngine
from app.feeds.random_walk import RandomWalkFeed, synthetic_nifty
from app.notify.base import LogNotifier
from app.strategies.registry import StrategyConfig

DAY = date(2025, 11, 3)
OPEN = datetime(2025, 11, 3, 9, 15, tzinfo=IST)
CLOSE = datetime(2025, 11, 3, 15, 30, tzinfo=IST)


async def run_day(
    configs: list[StrategyConfig], seed: int, **overrides: Any
) -> tuple[TradingEngine, LogNotifier]:
    # These tests exercise the engine over a whole day, not the beginner defaults.
    overrides.setdefault("daily_loss_limit", 10_000)
    settings = Settings(_env_file=None, **overrides)  # defaults: paper, sim
    assert not settings.is_live
    universe = synthetic_nifty(DAY)
    store = InstrumentStore()
    store.load(universe.all, DAY)
    feed = RandomWalkFeed(
        universe,
        realtime=False,
        start=OPEN,
        end=CLOSE,
        step=timedelta(seconds=5),
        seed=seed,
        annual_vol=0.25,
    )
    notifier = LogNotifier()
    engine, enabled = factory.build_engine(
        settings,
        instruments=store,
        feed=feed,
        clock=ManualClock(OPEN),
        notifier=notifier,
        strategy_configs=configs,
        realtime=False,
    )
    await engine.start(enabled)
    await asyncio.wait_for(feed.finished.wait(), 60)
    await engine.drain()
    await engine.stop()
    return engine, notifier


def assert_flat_and_clean(engine: TradingEngine) -> None:
    assert engine.positions.open_positions() == [], engine.positions.open_positions()
    assert engine.open_orders() == []
    assert all(o.status.is_terminal for o in engine.orders.values())
    for sid, slot in engine.slots.items():
        assert slot.state is not StrategyState.ERROR, (sid, slot.error)


def cfg(id_: str, strategy: str, **params: Any) -> StrategyConfig:
    return StrategyConfig(id=id_, strategy=strategy, enabled=True, params=params)


async def test_ema_crossover_full_day_ends_flat() -> None:
    engine, _ = await run_day(
        [cfg("ema", "ema_crossover", interval_minutes=1, fast=3, slow=8)], seed=11
    )
    assert_flat_and_clean(engine)
    fills = [f for f in engine.fills if f.strategy_id == "ema"]
    assert len(fills) >= 4, "expected several crossovers in a volatile day"
    assert all(f.instrument.instrument_type == "FUT" for f in fills)
    # Entries stop at last_entry_time (15:00); after that only exits.
    late_entries = [
        o
        for o in engine.orders.values()
        if o.request.reason.startswith("ema cross ")
        and "exit" not in o.request.reason
        and o.created_at.time() >= time(15, 0)
    ]
    assert late_entries == []
    pnl = engine.positions.pnl("ema")
    assert pnl.unrealized == 0
    assert pnl.realized != 0


async def test_short_straddle_full_day_ends_flat_via_time_exit() -> None:
    engine, _ = await run_day([cfg("straddle", "short_straddle")], seed=3)
    assert_flat_and_clean(engine)
    orders = [o for o in engine.orders.values() if o.request.strategy_id == "straddle"]
    entries = [o for o in orders if o.request.leg_group_id]
    assert len(entries) == 2  # one leg group: call + put
    assert len({o.request.leg_group_id for o in entries}) == 1
    (group,) = engine.legs.groups.values()
    assert group.status.value == "FILLED"
    assert {o.request.instrument.instrument_type for o in entries} == {"CE", "PE"}
    assert all(o.status is OrderStatus.COMPLETE for o in entries)
    assert {o.request.instrument.strike for o in entries} == {entries[0].request.instrument.strike}
    assert entries[0].created_at.time() >= time(9, 20)
    # Option MARKET requests went out as LIMIT orders priced from the quote.
    assert all(o.request.order_type == "LIMIT" for o in entries)
    # Each filled leg got an exchange-side SL order, cancelled again at the time exit.
    stops = [o for o in orders if o.request.protective]
    assert {o.request.instrument.token for o in stops} == {
        o.request.instrument.token for o in entries
    }
    assert all(o.request.order_type == "SL" and o.request.side == "BUY" for o in stops)
    assert all(o.status.is_terminal for o in stops)


async def test_both_strategies_square_off_flattens_everything() -> None:
    """Straddle exit_time after square-off, so the engine's 15:15 square-off must flatten."""
    engine, notifier = await run_day(
        [
            cfg(
                "ema", "ema_crossover", interval_minutes=1, fast=3, slow=8, last_entry_time="15:09"
            ),
            cfg("straddle", "short_straddle", exit_time="15:25", stop_loss_pct=200),
        ],
        seed=5,
        daily_loss_limit=10_000_000,  # this test is about square-off, not the kill switch
    )
    assert not engine.risk.kill_switch.tripped
    assert_flat_and_clean(engine)
    sq = [o for o in engine.orders.values() if o.request.reason == "scheduled square-off"]
    assert {o.request.strategy_id for o in sq} >= {"straddle"}
    assert all(o.created_at.time() >= time(15, 15) for o in sq)
    assert all(o.status is OrderStatus.COMPLETE for o in sq)
    assert any("Square-off" in m for _, m in notifier.sent)
    by = engine.positions.pnl_by_strategy()
    assert set(by) == {"ema", "straddle"}
