"""Liquidity rules, LIMIT conversion and chasing, exchange-side stops, expiry rules,
rolls, and freeze-limit checks."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from app.core.models import OrderStatus, OrderType, Product, Side
from app.data.instruments import weekdays_until
from app.engine.engine import EngineConfig
from app.engine.legs import LegGroupStatus
from app.engine.smart_orders import OptionOrderRules, limit_price
from tests.builders import CE, FUT, ist, tick
from tests.engine_kit import FUT_FAR, PE, STOCK_CE, ScriptedExecutor, build, feed, liquid

# --- liquidity & LIMIT conversion -------------------------------------------------------


async def test_option_market_order_becomes_marketable_limit() -> None:
    engine, s, _ = await build()
    await feed(engine, liquid(CE, 100.0))  # bid 99.95 / ask 100.05
    o = s.buy(CE, 75)
    engine._pump()
    assert o.request.order_type is OrderType.LIMIT
    assert o.request.price == pytest.approx(100.15)  # ask + 2 ticks
    assert o.status is OrderStatus.COMPLETE
    assert o.average_price == pytest.approx(100.05)  # paper fills at the ask
    await engine.stop()


@pytest.mark.parametrize(
    ("make_tick", "reason"),
    [
        (lambda: None, "no quote"),
        (lambda: tick(CE, 100.0, ist(10, 0)), "two-sided"),
        (lambda: tick(CE, 100.0, ist(10, 0), bid=90.0, ask=110.0, volume=10**6), "spread"),
        (lambda: tick(CE, 100.0, ist(10, 0), bid=99.95, ask=100.05, volume=10), "volume"),
    ],
)
async def test_illiquid_option_entries_rejected(make_tick: object, reason: str) -> None:
    engine, s, _ = await build(option_min_volume=1000)
    t = make_tick()  # type: ignore[operator]
    if t is not None:
        await feed(engine, t)
    o = s.sell(CE, 75)
    assert o.status is OrderStatus.REJECTED and reason in o.status_message
    await engine.stop()


async def test_option_exit_without_quote_stays_market_and_is_allowed() -> None:
    ex = ScriptedExecutor()
    engine, s, _ = await build(executor=ex)
    await feed(engine, liquid(CE, 100.0))
    ex.fill(s.sell(CE, 75), 100.0)
    engine._pump()
    engine._ticks.pop(CE.token)  # quote disappears
    (exit_order,) = s.exit_position(CE)
    assert exit_order.status is not OrderStatus.REJECTED
    assert exit_order.request.order_type is OrderType.MARKET
    await engine.stop()


def test_exit_limit_price_is_more_aggressive() -> None:
    rules = OptionOrderRules(buffer_ticks=2, exit_buffer_pct=2.0)
    q = liquid(CE, 100.0)
    assert limit_price(CE, Side.BUY, q, is_exit=False, step=0, rules=rules) == pytest.approx(100.15)
    assert limit_price(CE, Side.BUY, q, is_exit=True, step=0, rules=rules) == pytest.approx(102.05)
    assert limit_price(CE, Side.SELL, q, is_exit=True, step=1, rules=rules) == pytest.approx(95.95)


async def test_unfilled_option_limit_is_chased_then_given_up() -> None:
    ex = ScriptedExecutor()
    cfg = EngineConfig(option_rules=OptionOrderRules(chase_after_sec=5, chase_max=2))
    engine, s, notifier = await build(executor=ex, config=cfg)
    await feed(engine, liquid(CE, 100.0))
    o = s.buy(CE, 75)
    engine._pump()
    first = o.request.price
    for i in range(1, 4):
        engine.clock.advance(timedelta(seconds=5))
        await feed(engine, liquid(CE, 100.0 + i, ts=engine.clock.now()))
    assert len(ex.modified) == 2  # chase_max
    assert all(kw["price"] > (first or 0) for _, kw in ex.modified)
    await asyncio.sleep(0)
    assert any("stopped chasing" in m for _, m in notifier.sent)
    await engine.stop()


# --- exchange-side stops --------------------------------------------------------------------


async def test_protect_places_trails_resizes_and_releases_stop() -> None:
    engine, s, _ = await build()
    await feed(engine, liquid(CE, 100.0))
    s.buy(CE, 75)
    engine._pump()
    s.protect(CE, 80.0)
    engine._pump()
    stop = engine.stops.stop_for(("s", CE.token, Product.MIS))
    assert stop is not None and stop.request.protective
    assert (stop.request.side, stop.request.order_type, stop.request.quantity) == (
        Side.SELL,
        OrderType.SL,
        75,
    )
    assert stop.request.trigger_price == 80.0 and stop.request.price == 76.0  # 5% below
    assert stop.status is OrderStatus.TRIGGER_PENDING

    s.protect(CE, 90.0)  # trail
    assert stop.request.trigger_price == 90.0
    engine.clock.advance(timedelta(seconds=3))  # past the duplicate-order guard
    s.buy(CE, 75)  # add to the position -> stop quantity follows
    engine._pump()
    assert stop.request.quantity == 150

    exits = s.exit_position(CE)  # stop is released first, then the exit
    engine._pump()
    assert stop.status is OrderStatus.CANCELLED
    assert exits[0].status is OrderStatus.COMPLETE
    assert engine.positions.net_quantity("s", CE.token) == 0
    await feed(engine, liquid(CE, 101.0, ts=ist(10, 1)))
    assert engine.stops.stop_for(("s", CE.token, Product.MIS)) is None  # not re-placed
    await engine.stop()


async def test_exchange_stop_triggers_and_flattens() -> None:
    engine, s, _ = await build()
    await feed(engine, liquid(CE, 100.0))
    s.sell(CE, 75)
    engine._pump()
    s.protect(CE, 120.0)  # short: buy stop above
    engine._pump()
    await feed(engine, liquid(CE, 121.0, ts=ist(10, 1)))
    assert engine.positions.net_quantity("s", CE.token) == 0
    assert engine.stops.protected() == {}
    await engine.stop()


async def test_resting_stop_never_blocks_an_exit_under_kill_switch() -> None:
    ex = ScriptedExecutor()
    engine, s, _ = await build(executor=ex)
    await feed(engine, liquid(CE, 100.0))
    ex.fill(s.buy(CE, 75), 100.0)
    engine._pump()
    s.protect(CE, 80.0)
    engine._pump()
    engine.risk.kill_switch.trip("t", engine.clock.now())
    from app.core.models import OrderRequest  # noqa: PLC0415

    d = engine.risk.check(OrderRequest("s", CE, Side.SELL, 75), engine.open_orders())
    assert d.allowed and d.is_exit
    await engine.stop()


async def test_square_off_releases_stops_and_does_not_replace_them() -> None:
    engine, s, _ = await build()
    await feed(engine, liquid(CE, 100.0))
    s.buy(CE, 75)
    engine._pump()
    s.protect(CE, 80.0)
    engine._pump()
    engine.trip_kill_switch("manual")
    await feed(engine, liquid(CE, 99.0, ts=ist(10, 1)))
    assert engine.positions.open_positions() == []
    assert engine.stops.protected() == {}
    stops = [o for o in engine.orders.values() if o.request.protective]
    assert len(stops) == 1 and stops[0].status is OrderStatus.CANCELLED
    await engine.stop()


# --- expiry ----------------------------------------------------------------------------------


async def test_no_entries_on_expiry_day_after_cutoff_but_exits_allowed() -> None:
    expiry_day = date(2025, 11, 11)
    engine, s, _ = await build(d=expiry_day, h=14)
    await feed(engine, liquid(CE, 100.0, ts=ist(14, 0, d=expiry_day)))
    s.buy(CE, 75)
    engine._pump()
    engine.clock.set(ist(14, 31, d=expiry_day))
    o = s.buy(CE, 75)
    assert o.status is OrderStatus.REJECTED and "expiry day" in o.status_message
    assert s.exit_position(CE)[0].status is not OrderStatus.REJECTED
    await engine.stop()


def test_weekdays_until() -> None:
    assert weekdays_until(date(2025, 11, 24), date(2025, 11, 25)) == 1  # Mon -> Tue
    assert weekdays_until(date(2025, 11, 21), date(2025, 11, 24)) == 1  # Fri -> Mon
    assert weekdays_until(date(2025, 11, 25), date(2025, 11, 25)) == 0
    assert weekdays_until(date(2025, 11, 20), date(2025, 11, 25)) == 3


async def test_stock_option_exited_before_physical_settlement() -> None:
    day_before = date(2025, 11, 24)  # STOCK_CE expires Tue 25th
    engine, s, notifier = await build(d=day_before, h=14)
    await feed(
        engine,
        liquid(STOCK_CE, 20.0, ts=ist(14, 0, d=day_before)),
        liquid(CE, 50.0, ts=ist(14, 0, d=day_before)),
    )
    s.buy(STOCK_CE, 500)
    s.buy(CE, 75)  # index option: cash-settled, must NOT be auto-exited
    engine._pump()
    await feed(engine, liquid(STOCK_CE, 20.0, ts=ist(14, 59, d=day_before)))
    assert engine.positions.net_quantity("s", STOCK_CE.token) == 500
    await feed(engine, liquid(STOCK_CE, 20.0, ts=ist(15, 0, d=day_before)))
    assert engine.positions.net_quantity("s", STOCK_CE.token) == 0
    assert engine.positions.net_quantity("s", CE.token) == 75
    await asyncio.sleep(0)
    assert any("physical settlement" in m for _, m in notifier.sent)
    await engine.stop()


async def test_roll_future_to_next_expiry_as_leg_group() -> None:
    engine, s, _ = await build()
    await feed(engine, liquid(FUT, 24000.0), liquid(FUT_FAR, 24100.0))
    s.buy(FUT, 150, product=Product.NRML)
    engine._pump()
    assert engine.instruments.next_contract(FUT) == FUT_FAR
    with pytest.raises(LookupError):
        engine.instruments.next_contract(CE)  # no later option expiry in this test universe
    group = s.roll(FUT)
    engine._pump()
    assert group is not None and group.status is LegGroupStatus.FILLED
    assert engine.positions.net_quantity("s", FUT.token) == 0
    far = engine.positions.get("s", FUT_FAR.token, Product.NRML)
    assert far is not None and far.quantity == 150
    await engine.stop()


async def test_freeze_limit_caps_order_size() -> None:
    engine, s, _ = await build(freeze_limits={"NIFTY": 1800}, max_lots_per_order=1000)
    await feed(engine, liquid(FUT, 24000.0))
    o = s.buy(FUT, 18_075)  # > 10 slices of 1800
    assert o.status is OrderStatus.REJECTED and "freeze limit" in o.status_message
    assert s.buy(FUT, 3600).status is not OrderStatus.REJECTED
    await engine.stop()


_ = PE
