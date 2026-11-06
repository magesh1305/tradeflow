"""Leg groups: every failure path must leave the strategy flat (or alert loudly)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.core.models import OrderStatus, Side
from app.engine.legs import LegGroup, LegGroupStatus, LegSpec
from app.risk.margin import EstimatedMargin
from app.storage.journal import Journal, MemoryJournalStore
from tests.builders import CE, ist, tick
from tests.engine_kit import CE_HI, PE, ScriptedExecutor, build, feed, liquid


def spread() -> list[LegSpec]:
    """Short call spread: sell 24000 CE, buy 24200 CE."""
    return [LegSpec(CE, Side.SELL, 75), LegSpec(CE_HI, Side.BUY, 75)]


async def quotes(engine: object) -> None:
    await feed(engine, liquid(CE, 100.0), liquid(CE_HI, 40.0), liquid(PE, 90.0))  # type: ignore[arg-type]


async def test_all_legs_fill_and_hedge_goes_first() -> None:
    engine, s, _ = await build(journal=True)
    await quotes(engine)
    seen: list[LegGroup] = []
    s.on_legs = seen.append  # type: ignore[method-assign,assignment]
    group = s.place_legs(*spread())
    engine._pump()
    assert group.status is LegGroupStatus.FILLED
    assert [o.request.side for o in group.legs] == [Side.BUY, Side.SELL]  # hedge first
    assert engine.positions.net_quantity("s", CE.token) == -75
    assert engine.positions.net_quantity("s", CE_HI.token) == 75
    assert all(o.request.leg_group_id == group.id for o in group.legs)
    assert seen and seen[-1].status is LegGroupStatus.FILLED
    j = engine.journal
    assert isinstance(j, Journal)
    await j.flush()
    store = j.store
    assert isinstance(store, MemoryJournalStore)
    assert store.leg_groups[group.id]["status"] == "FILLED"
    await engine.stop()


async def test_risk_rejection_of_one_leg_sends_nothing() -> None:
    ex = ScriptedExecutor()
    engine, s, _ = await build(executor=ex)
    await feed(engine, liquid(CE, 100.0))  # no quote for CE_HI -> liquidity rejection
    group = s.place_legs(*spread())
    engine._pump()
    assert group.status is LegGroupStatus.REJECTED
    assert "no quote" in group.error
    assert ex.submitted == []
    assert all(o.status is OrderStatus.REJECTED for o in group.legs)
    await engine.stop()


async def test_broker_rejects_a_leg_and_filled_leg_is_unwound() -> None:
    ex = ScriptedExecutor()
    engine, s, notifier = await build(executor=ex)
    await quotes(engine)
    group = s.place_legs(*spread())
    buy_leg, sell_leg = group.legs
    ex.fill(buy_leg, 40.0)
    ex.reject(sell_leg, "Insufficient margin")
    engine._pump()
    assert group.status is LegGroupStatus.UNWINDING
    assert "Insufficient margin" in group.error
    (unwind,) = [o for o in ex.submitted if o.request.reason == "leg group unwind"]
    assert (unwind.request.side, unwind.request.quantity) == (Side.SELL, 75)
    ex.fill(unwind, 39.0)
    engine._pump()
    assert group.status is LegGroupStatus.UNWOUND
    assert engine.positions.open_positions() == []
    await asyncio.sleep(0)
    assert any(lvl == "critical" and "Leg group failed" in m for lvl, m in notifier.sent)
    await engine.stop()


async def test_timeout_cancels_pending_and_unwinds_partial_fill() -> None:
    ex = ScriptedExecutor()
    engine, s, _ = await build(executor=ex)
    await quotes(engine)
    group = s.place_legs(LegSpec(CE, Side.SELL, 150), LegSpec(PE, Side.SELL, 150), timeout_sec=10)
    ce_leg, pe_leg = group.legs
    ex.fill(ce_leg, 100.0, qty=75)  # partial
    engine._pump()
    assert group.status is LegGroupStatus.WORKING
    engine.clock.advance(timedelta(seconds=11))
    engine._on_time(engine.clock.now())
    assert group.status is LegGroupStatus.UNWINDING
    assert "not filled within" in group.error
    assert set(ex.cancelled) == {ce_leg.client_order_id, pe_leg.client_order_id}
    (unwind,) = [o for o in ex.submitted if o.request.reason == "leg group unwind"]
    assert (unwind.request.side, unwind.request.quantity) == (Side.BUY, 75)  # only the filled part
    ex.fill(unwind, 101.0)
    engine._pump()
    assert group.status is LegGroupStatus.UNWOUND
    assert engine.positions.open_positions() == []
    await engine.stop()


async def test_unwind_failure_alerts_critically() -> None:
    ex = ScriptedExecutor()
    engine, s, notifier = await build(executor=ex)
    await quotes(engine)
    group = s.place_legs(*spread())
    buy_leg, sell_leg = group.legs
    ex.fill(buy_leg, 40.0)
    ex.reject(sell_leg)
    engine._pump()
    (unwind,) = [o for o in ex.submitted if o.request.reason == "leg group unwind"]
    ex.reject(unwind, "circuit limit")
    engine._pump()
    await asyncio.sleep(0)
    assert group.status is LegGroupStatus.UNWIND_FAILED
    assert any(lvl == "critical" and "UNWIND FAILED" in m for lvl, m in notifier.sent)
    await engine.stop()


async def test_all_legs_rejected_needs_no_unwind() -> None:
    ex = ScriptedExecutor()
    engine, s, _ = await build(executor=ex)
    await quotes(engine)
    group = s.place_legs(*spread())
    for leg in group.legs:
        ex.reject(leg)
    engine._pump()
    assert group.status is LegGroupStatus.UNWOUND
    assert not [o for o in ex.submitted if o.request.reason == "leg group unwind"]
    await engine.stop()


async def test_kill_switch_aborts_group_without_double_exit() -> None:
    ex = ScriptedExecutor()
    engine, s, _ = await build(executor=ex)
    await quotes(engine)
    group = s.place_legs(*spread())
    buy_leg, _sell_leg = group.legs
    ex.fill(buy_leg, 40.0)
    engine._pump()
    engine.trip_kill_switch("manual")
    assert group.status is LegGroupStatus.ABORTED
    exits = [
        o
        for o in ex.submitted
        if o.request.side is Side.SELL and o.request.instrument.token == CE_HI.token
    ]
    assert len(exits) == 1  # square-off only; no second (unwind) exit
    assert exits[0].request.reason.startswith("kill switch")
    await engine.stop()


async def test_margin_shortfall_rejects_whole_group() -> None:
    ex = ScriptedExecutor()
    engine, s, _ = await build(executor=ex)
    engine.margin = EstimatedMargin(50_000, 0.15, engine.last_tick)
    await quotes(engine)
    group = s.place_legs(LegSpec(CE, Side.SELL, 75), LegSpec(PE, Side.SELL, 75))
    engine._pump()
    assert group.status is LegGroupStatus.REJECTED
    assert "insufficient margin" in group.error
    assert ex.submitted == []
    await engine.stop()


async def test_paper_fill_on_later_tick_completes_group() -> None:
    engine, s, _ = await build()
    await quotes(engine)
    group = s.place_legs(*spread())
    engine._pump()
    assert group.status is LegGroupStatus.FILLED
    await feed(engine, tick(CE, 100.0, ist(10, 1)))
    assert group.status is LegGroupStatus.FILLED
    await engine.stop()
