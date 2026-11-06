from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import pytest

from app.core.clock import ManualClock
from app.core.models import Candle, Order, OrderStatus, Product, Side, Tick
from app.data.instruments import InstrumentStore
from app.engine.engine import EngineConfig, StrategyState, TradingEngine
from app.engine.positions import PositionBook
from app.execution.paper import PaperExecutor
from app.feeds.base import FeedSink
from app.notify.base import LogNotifier
from app.risk.manager import RiskLimits, RiskManager
from app.strategies.base import Strategy
from tests.builders import CE, FUT, ist, tick


class ManualFeed:
    def __init__(self) -> None:
        self.sink: FeedSink | None = None
        self.subscribed: set[int] = set()
        self.connected = True

    async def start(self, sink: FeedSink) -> None:
        self.sink = sink

    async def stop(self) -> None:
        self.connected = False

    def subscribe(self, tokens: Iterable[int]) -> None:
        self.subscribed.update(tokens)

    def unsubscribe(self, tokens: Iterable[int]) -> None:
        self.subscribed.difference_update(tokens)


class Recorder(Strategy):
    name = "recorder"

    def __init__(self, sid: str, token: int = FUT.token, candles: int | None = None) -> None:
        super().__init__(sid)
        self.token, self.candle_interval = token, candles
        self.ticks: list[Tick] = []
        self.candles: list[Candle] = []
        self.orders: list[Order] = []
        self.started = self.stopped = False

    def on_start(self) -> None:
        self.started = True
        self.ctx.subscribe([self.token])
        if self.candle_interval:
            self.ctx.subscribe_candles(self.token, self.candle_interval)

    def on_tick(self, t: Tick) -> None:
        self.ticks.append(t)

    def on_candle(self, c: Candle) -> None:
        self.candles.append(c)

    def on_order(self, o: Order) -> None:
        self.orders.append(o)

    def on_stop(self) -> None:
        self.stopped = True


class Boom(Recorder):
    def on_tick(self, t: Tick) -> None:
        raise ZeroDivisionError("bad math")


async def make(**limits: Any) -> tuple[TradingEngine, ManualFeed, LogNotifier]:
    clock = ManualClock(ist(9, 15))
    store = InstrumentStore()
    store.load([FUT, CE], clock.now().date())
    positions = PositionBook()
    feed = ManualFeed()
    notifier = LogNotifier()
    engine = TradingEngine(
        instruments=store,
        feed=feed,
        executor=PaperExecutor(clock, slippage_ticks=0),
        risk=RiskManager(RiskLimits(**limits), positions, clock),
        positions=positions,
        clock=clock,
        notifier=notifier,
        config=EngineConfig(),
    )
    return engine, feed, notifier


async def push(engine: TradingEngine, *ticks: Tick) -> None:
    engine.on_ticks(list(ticks))
    await engine.drain()
    await asyncio.sleep(0)  # let alert tasks run


async def test_routes_ticks_and_candles_only_to_subscribers() -> None:
    engine, feed, _ = await make()
    a = Recorder("a", FUT.token, candles=60)
    b = Recorder("b", CE.token)
    engine.add_strategy(a)
    engine.add_strategy(b)
    await engine.start(["a", "b"])
    assert feed.subscribed == {FUT.token, CE.token}
    await push(engine, tick(FUT, 100, ist(9, 15, 1)), tick(FUT, 101, ist(9, 16, 1)))
    assert [t.ltp for t in a.ticks] == [100, 101]
    assert b.ticks == []
    assert len(a.candles) == 1 and a.candles[0].close == 100
    assert engine.clock.now() == ist(9, 16, 1)  # market time from ticks
    await engine.stop()
    assert a.stopped


async def test_strategy_exception_disables_only_that_strategy() -> None:
    engine, _, notifier = await make()
    good, bad = Recorder("good"), Boom("bad")
    engine.add_strategy(good)
    engine.add_strategy(bad)
    await engine.start(["good", "bad"])
    await push(engine, tick(FUT, 100, ist(9, 20)), tick(FUT, 101, ist(9, 20, 1)))
    assert engine.slots["bad"].state is StrategyState.ERROR
    assert "ZeroDivisionError" in engine.slots["bad"].error
    assert engine.slots["good"].state is StrategyState.RUNNING
    assert len(good.ticks) == 2
    assert any(lvl == "critical" and "bad" in msg for lvl, msg in notifier.sent)
    await engine.stop()


async def test_rejected_order_reaches_strategy_and_alerts() -> None:
    engine, _, notifier = await make()
    s = Recorder("s")
    engine.add_strategy(s)
    await engine.start(["s"])
    engine.clock.set(ist(15, 12))  # outside the entry window
    o = s.buy(FUT, 75)
    engine._pump()
    await asyncio.sleep(0)
    assert o.status is OrderStatus.REJECTED
    assert "trading window" in o.status_message
    assert s.orders[-1].status is OrderStatus.REJECTED
    assert any("rejected" in msg for _, msg in notifier.sent)
    await engine.stop()


async def test_strategy_cannot_submit_for_another_or_when_stopped() -> None:
    engine, _, _ = await make()
    s = Recorder("s")
    engine.add_strategy(s)
    await engine.start(["s"])
    from app.core.models import OrderRequest  # noqa: PLC0415

    with pytest.raises(ValueError, match="themselves"):
        s.ctx.submit(OrderRequest("other", FUT, Side.BUY, 75))
    engine.stop_strategy("s")
    with pytest.raises(RuntimeError, match="STOPPED"):
        s.buy(FUT, 75)
    await engine.stop()


async def test_daily_loss_trips_kill_switch_and_flattens() -> None:
    engine, _, notifier = await make(daily_loss_limit=1000)
    s = Recorder("s")
    engine.add_strategy(s)
    await engine.start(["s"])
    await push(engine, tick(FUT, 100, ist(9, 20)))
    s.buy(FUT, 75)
    engine._pump()
    assert engine.positions.net_quantity("s", FUT.token) == 75
    await push(engine, tick(FUT, 85, ist(9, 21)))  # -15 * 75 = -1125
    assert engine.risk.kill_switch.tripped
    assert engine.slots["s"].state is StrategyState.STOPPED
    assert engine.positions.net_quantity("s", FUT.token) == 0
    assert any(lvl == "critical" and "KILL" in msg for lvl, msg in notifier.sent)
    with pytest.raises(RuntimeError, match="kill switch"):
        engine.start_strategy("s")
    engine.reset_kill_switch()
    assert engine.slots["s"].state is StrategyState.STOPPED  # stays stopped
    engine.start_strategy("s")
    assert engine.slots["s"].state is StrategyState.RUNNING
    await engine.stop()


async def test_manual_kill_switch_cancels_entries() -> None:
    engine, _, _ = await make()
    s = Recorder("s")
    engine.add_strategy(s)
    await engine.start(["s"])
    await push(engine, tick(FUT, 100, ist(9, 20)))
    from app.core.models import OrderType  # noqa: PLC0415

    resting = s.buy(FUT, 75, order_type=OrderType.LIMIT, price=90.0)
    engine.trip_kill_switch("manual")
    assert resting.status is OrderStatus.CANCELLED
    await engine.stop()


async def test_scheduled_square_off_is_mis_only_and_uses_position_product() -> None:
    engine, _, _ = await make()
    s = Recorder("s")
    engine.add_strategy(s)
    await engine.start(["s"])
    await push(engine, tick(FUT, 100, ist(10, 0)))
    s.sell(FUT, 150, product=Product.NRML)
    s.buy(FUT, 75)
    engine._pump()
    await push(engine, tick(FUT, 99, ist(15, 14, 59)))
    assert len(engine.positions.open_positions()) == 2
    await push(engine, tick(FUT, 98, ist(15, 15)))
    # Scheduled square-off closes MIS only; the NRML position is carried.
    (carried,) = engine.positions.open_positions()
    assert (carried.product, carried.quantity) == (Product.NRML, -150)
    exits = [o for o in engine.orders.values() if o.request.reason == "scheduled square-off"]
    assert [(o.request.product, o.request.side) for o in exits] == [(Product.MIS, Side.SELL)]
    # A manual / kill-switch square-off flattens everything, using the NRML product.
    engine.square_off_all("manual")
    assert engine.positions.open_positions() == []
    (nrml_exit,) = [o for o in engine.orders.values() if o.request.reason == "manual"]
    assert (nrml_exit.request.product, nrml_exit.request.side) == (Product.NRML, Side.BUY)
    await engine.stop()


async def test_stop_strategy_with_exit() -> None:
    engine, _, _ = await make()
    a, b = Recorder("a"), Recorder("b")
    engine.add_strategy(a)
    engine.add_strategy(b)
    await engine.start(["a", "b"])
    await push(engine, tick(FUT, 100, ist(10, 0)))
    a.buy(FUT, 75)
    b.sell(FUT, 75)
    engine._pump()
    engine.stop_strategy("a", exit_positions=True)
    assert engine.positions.net_quantity("a", FUT.token) == 0
    assert engine.positions.net_quantity("b", FUT.token) == -75
    await engine.stop()
