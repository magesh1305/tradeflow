"""TradingEngine: the single-loop event core.

feed -> tick queue -> last-price cache / positions mark / executor (paper fills)
     -> candle aggregator -> strategies (on_tick / on_candle)
strategies -> submit -> RiskManager -> Executor -> order updates and fills
     -> PositionBook -> P&L check (>= once per market second) -> kill switch

Market time comes from tick timestamps (a ManualClock that only moves forward).
In realtime mode a 1-second wall-clock timer also advances it, so time-based
actions (square-off, P&L checks) still fire when ticks pause.

Executor callbacks are queued and processed after the current dispatch
(`_pump`), so strategy hooks are never re-entered.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Protocol

from app.core.clock import Clock, ManualClock
from app.core.models import (
    Candle,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Product,
    Side,
    Tick,
)
from app.data.instruments import InstrumentStore
from app.engine.candles import CandleAggregator
from app.engine.positions import Position, PositionBook
from app.execution.base import Executor
from app.feeds.base import Feed
from app.notify.base import Level, Notifier
from app.risk.manager import RiskManager
from app.strategies.base import Strategy

log = logging.getLogger(__name__)

SYSTEM = "__system__"


class OrderJournal(Protocol):
    def record_order(self, order: Order) -> None: ...
    def record_fill(self, fill: Fill, charges: float = 0.0) -> None: ...


class NullJournal:
    def record_order(self, order: Order) -> None:
        pass

    def record_fill(self, fill: Fill, charges: float = 0.0) -> None:
        pass


class EngineBlockedError(RuntimeError):
    pass


class StrategyState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    ERROR = "ERROR"


class StrategyNotRunningError(RuntimeError):
    pass


@dataclass
class StrategySlot:
    strategy: Strategy
    state: StrategyState = StrategyState.STOPPED
    error: str = ""
    tick_tokens: set[int] = field(default_factory=set)
    candle_subs: set[tuple[int, int]] = field(default_factory=set)


@dataclass(frozen=True)
class EngineConfig:
    square_off_time: time = time(15, 15)
    market_open: time = time(9, 15)
    pnl_check_interval: timedelta = timedelta(seconds=1)


class _Context:
    """What a strategy sees. One per strategy; forwards to the engine."""

    def __init__(self, engine: TradingEngine, slot: StrategySlot) -> None:
        self._engine = engine
        self._slot = slot
        self.strategy_id = slot.strategy.id
        self.instruments: InstrumentStore = engine.instruments
        self.log = logging.getLogger(f"strategy.{self.strategy_id}")

    def now(self) -> datetime:
        return self._engine.clock.now()

    @property
    def today(self) -> date:
        return self._engine.clock.now().date()

    def ltp(self, token: int) -> float | None:
        t = self._engine.last_tick(token)
        return t.ltp if t else None

    def tick(self, token: int) -> Tick | None:
        return self._engine.last_tick(token)

    def positions(self) -> list[Position]:
        return self._engine.positions.positions(self.strategy_id)

    def open_orders(self) -> list[Order]:
        return [o for o in self._engine.open_orders() if o.request.strategy_id == self.strategy_id]

    def subscribe(self, tokens: list[int]) -> None:
        self._engine.subscribe_ticks(self._slot, tokens)

    def subscribe_candles(self, token: int, interval_seconds: int) -> None:
        self._engine.subscribe_candles(self._slot, token, interval_seconds)

    def submit(self, req: OrderRequest) -> Order:
        if self._slot.state is not StrategyState.RUNNING:
            raise StrategyNotRunningError(f"{self.strategy_id} is {self._slot.state}")
        if req.strategy_id != self.strategy_id:
            raise ValueError("strategies may only submit orders for themselves")
        return self._engine.submit_order(req)

    def cancel(self, client_order_id: str) -> None:
        order = self._engine.orders.get(client_order_id)
        if order is not None and order.request.strategy_id == self.strategy_id:
            self._engine.executor.cancel(client_order_id)


class TradingEngine:
    def __init__(
        self,
        *,
        instruments: InstrumentStore,
        feed: Feed,
        executor: Executor,
        risk: RiskManager,
        positions: PositionBook,
        clock: ManualClock,
        notifier: Notifier,
        config: EngineConfig | None = None,
        wall_clock: Clock | None = None,
        journal: OrderJournal | None = None,
    ) -> None:
        self.instruments = instruments
        self.feed = feed
        self.executor = executor
        self.risk = risk
        self.positions = positions
        self.clock = clock
        self.notifier = notifier
        self.config = config or EngineConfig()
        self._wall_clock = wall_clock
        self.journal: OrderJournal = journal or NullJournal()
        # Set by crash recovery when our state disagrees with the broker. While set,
        # no strategy can start until a human resolves it through the API.
        self.blocked_reason = ""
        self._hook_depth = 0
        self.candles = CandleAggregator(self.config.market_open)
        self.slots: dict[str, StrategySlot] = {}
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self._ticks: dict[int, Tick] = {}
        self._queue: asyncio.Queue[Tick] = asyncio.Queue()
        self._events: deque[Callable[[], None]] = deque()
        self._pumping = False
        self._tasks: list[asyncio.Task[None]] = []
        self._bg: set[asyncio.Task[None]] = set()
        self._last_pnl_check: datetime | None = None
        self._squared_off_on: date | None = None
        self.feed_connected = False
        self.feed_detail = ""
        self.running = False
        executor.bind(_ExecutionAdapter(self))

    # --- lifecycle -------------------------------------------------------------

    def add_strategy(self, strategy: Strategy) -> StrategySlot:
        if strategy.id in self.slots:
            raise ValueError(f"duplicate strategy id {strategy.id}")
        slot = StrategySlot(strategy)
        self.slots[strategy.id] = slot
        strategy.bind(_Context(self, slot))
        return slot

    async def start(self, enabled: list[str] | None = None) -> None:
        self.running = True
        await self.executor.start()
        await self.feed.start(self)
        self._tasks.append(asyncio.create_task(self._consume(), name="engine-consume"))
        if self._wall_clock is not None:
            self._tasks.append(asyncio.create_task(self._timer(), name="engine-timer"))
        if self.blocked_reason:
            log.critical(
                "engine blocked; strategies not started", extra={"reason": self.blocked_reason}
            )
        else:
            for sid in enabled or []:
                self.start_strategy(sid)
        self._alert(f"Engine started ({len(self.slots)} strategies)", "info")

    async def stop(self) -> None:
        for slot in self.slots.values():
            if slot.state is StrategyState.RUNNING:
                self._call(slot, slot.strategy.on_stop)
                slot.state = StrategyState.STOPPED
        await self.feed.stop()
        await self.executor.stop()
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
        self.running = False
        self._alert("Engine stopped", "info")
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)

    async def drain(self) -> None:
        """Wait until every queued tick has been processed."""
        await self._queue.join()

    # --- FeedSink ----------------------------------------------------------------

    def on_ticks(self, ticks: list[Tick]) -> None:
        for t in ticks:
            self._queue.put_nowait(t)

    def on_order_update(self, payload: dict[str, Any]) -> None:
        handler = getattr(self.executor, "on_broker_update", None)
        if handler is not None:
            self._post(lambda: handler(payload))
            self._pump()

    def on_feed_status(self, connected: bool, detail: str) -> None:
        was = self.feed_connected
        self.feed_connected, self.feed_detail = connected, detail
        if was and not connected:
            self._alert(f"Market data feed disconnected: {detail}", "warning")

    # --- ExecutionListener ------------------------------------------------------------

    def last_tick(self, token: int) -> Tick | None:
        return self._ticks.get(token)

    def on_fill(self, fill: Fill) -> None:
        # Positions update immediately so the next risk check sees them (a paper
        # fill can happen inside submit()). Only strategy callbacks are deferred.
        self.fills.append(fill)
        self.positions.apply_fill(fill)
        self.journal.record_fill(fill)
        self._post(lambda: self._handle_fill(fill))
        self._pump_if_idle()

    def _executor_order_update(self, order: Order) -> None:
        snapshot = dataclasses.replace(order)
        self.journal.record_order(snapshot)
        self._post(lambda: self._handle_order_update(snapshot))
        self._pump_if_idle()

    def _pump_if_idle(self) -> None:
        # Live executor callbacks arrive from background tasks, outside any dispatch,
        # so pump now. Never pump while a strategy hook is running (no re-entry).
        if self._hook_depth == 0:
            self._pump()

    # --- tick processing -----------------------------------------------------------

    async def _consume(self) -> None:
        while True:
            tick = await self._queue.get()
            try:
                self._process_tick(tick)
            except Exception:
                log.exception("engine failed to process tick", extra={"token": tick.token})
            finally:
                self._queue.task_done()

    def _process_tick(self, tick: Tick) -> None:
        self._ticks[tick.token] = tick
        if tick.ts > self.clock.now():
            self.clock.set(tick.ts)
        self.positions.mark(tick.token, tick.ltp)
        self.executor.on_tick(tick)
        self._pump()
        for candle in self.candles.on_tick(tick):
            self._dispatch_candle(candle)
        for slot in self.slots.values():
            if tick.token in slot.tick_tokens:
                self._call(slot, slot.strategy.on_tick, tick)
        self._pump()
        self._on_time(self.clock.now())

    async def _timer(self) -> None:
        assert self._wall_clock is not None  # noqa: S101 - only started when set
        while True:
            await asyncio.sleep(1.0)
            try:
                now = self._wall_clock.now()
                if now > self.clock.now():
                    self.clock.set(now)
                self._on_time(self.clock.now())
            except Exception:
                log.exception("engine timer failed")

    def _on_time(self, now: datetime) -> None:
        for candle in self.candles.flush(now):
            self._dispatch_candle(candle)
        self._pump()
        if (
            self._last_pnl_check is None
            or now - self._last_pnl_check >= self.config.pnl_check_interval
        ):
            self._last_pnl_check = now
            self.check_pnl()
        if now.time() >= self.config.square_off_time:
            self.scheduled_square_off()

    def scheduled_square_off(self) -> None:
        """Daily MIS square-off. Idempotent per day (engine timer and scheduler both call it)."""
        today = self.clock.now().date()
        if self._squared_off_on == today:
            return
        self._squared_off_on = today
        self.square_off_all("scheduled square-off", frozenset({Product.MIS}))

    def _dispatch_candle(self, candle: Candle) -> None:
        key = (candle.token, candle.interval)
        for slot in self.slots.values():
            if key in slot.candle_subs:
                self._call(slot, slot.strategy.on_candle, candle)

    # --- strategy management ---------------------------------------------------------

    def _call(self, slot: StrategySlot, fn: Callable[..., None], *args: Any) -> None:
        """Run a strategy hook; any exception disables only that strategy."""
        if slot.state is not StrategyState.RUNNING:
            return
        self._hook_depth += 1
        try:
            fn(*args)
        except Exception as exc:
            slot.state = StrategyState.ERROR
            slot.error = f"{type(exc).__name__}: {exc}"
            log.exception("strategy failed; disabled", extra={"strategy": slot.strategy.id})
            for o in self.open_orders():
                if o.request.strategy_id == slot.strategy.id:
                    self.executor.cancel(o.client_order_id)
            self._alert(
                f"Strategy {slot.strategy.id} disabled after error: {slot.error}", "critical"
            )
        finally:
            self._hook_depth -= 1

    def start_strategy(self, strategy_id: str) -> None:
        slot = self.slots[strategy_id]
        if self.blocked_reason:
            raise EngineBlockedError(f"engine blocked: {self.blocked_reason}")
        if self.risk.kill_switch.tripped:
            raise RuntimeError("kill switch is active; reset it before starting strategies")
        if slot.state is StrategyState.RUNNING:
            return
        slot.state, slot.error = StrategyState.RUNNING, ""
        self._call(slot, slot.strategy.on_start)
        self._pump()
        log.info("strategy started", extra={"strategy": strategy_id})

    def stop_strategy(self, strategy_id: str, exit_positions: bool = False) -> None:
        slot = self.slots[strategy_id]
        if slot.state is StrategyState.RUNNING:
            self._call(slot, slot.strategy.on_stop)
        slot.state = StrategyState.STOPPED
        for o in self.open_orders():
            if o.request.strategy_id == strategy_id:
                self.executor.cancel(o.client_order_id)
        if exit_positions:
            for pos in self.positions.open_positions(strategy_id):
                self._exit(pos, "strategy stopped")
        self._pump()
        log.info("strategy stopped", extra={"strategy": strategy_id, "exit": exit_positions})

    def subscribe_ticks(self, slot: StrategySlot, tokens: list[int]) -> None:
        slot.tick_tokens.update(tokens)
        self.feed.subscribe(tokens)

    def subscribe_candles(self, slot: StrategySlot, token: int, interval: int) -> None:
        self.candles.add(token, interval)
        slot.candle_subs.add((token, interval))
        self.feed.subscribe([token])

    # --- orders -------------------------------------------------------------------------

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.status.is_terminal]

    def submit_order(self, req: OrderRequest) -> Order:
        """The only path to an executor. Every order is risk-checked here."""
        now = self.clock.now()
        order = Order(req, created_at=now, updated_at=now)
        # Risk-check BEFORE recording the order: otherwise the new order counts as
        # its own "pending exit" and a genuine exit is misclassified as an entry.
        decision = self.risk.check(req, self.open_orders())
        self.orders[order.client_order_id] = order
        if not decision.allowed:
            order.status = OrderStatus.REJECTED
            order.status_message = f"risk: {decision.reason}"
            self._executor_order_update(order)
            self._alert(
                f"Order rejected [{req.strategy_id}] {req.side} {req.quantity} "
                f"{req.instrument.tradingsymbol}: {decision.reason}",
                "warning",
            )
            return order
        # Journal the intent (with its tag) before the broker sees it, so crash
        # recovery can match whatever Kite ends up holding.
        self.journal.record_order(order)
        self.executor.submit(order)
        return order

    def _exit(self, pos: Position, reason: str) -> Order | None:
        side = Side.SELL if pos.quantity > 0 else Side.BUY
        in_flight = sum(
            o.pending_quantity
            for o in self.open_orders()
            if o.request.strategy_id == pos.strategy_id
            and o.request.instrument.token == pos.instrument.token
            and o.request.product == pos.product
            and o.request.side == side
        )
        qty = abs(pos.quantity) - in_flight
        if qty <= 0:
            return None
        return self.submit_order(
            OrderRequest(
                strategy_id=pos.strategy_id,
                instrument=pos.instrument,
                side=side,
                quantity=qty,
                order_type=OrderType.MARKET,
                product=pos.product,  # square-off uses the position's own product
                reason=reason,
            )
        )

    def square_off_all(
        self, reason: str = "square-off", products: frozenset[Product] | None = None
    ) -> None:
        """Cancel open orders and flatten positions. `products=None` means everything
        (kill switch, manual); the scheduled square-off passes {MIS} so NRML carries."""
        for o in self.open_orders():
            if products is None or o.request.product in products:
                self.executor.cancel(o.client_order_id)
        self._pump()
        for pos in self.positions.open_positions():
            if products is None or pos.product in products:
                self._exit(pos, reason)
        self._pump()
        log.warning("square-off", extra={"reason": reason})
        self._alert(f"Square-off: {reason}", "warning")

    # --- kill switch & P&L --------------------------------------------------------------------

    def check_pnl(self) -> None:
        pnl = self.positions.pnl()
        if self.risk.check_daily_loss(pnl.net):
            self._on_kill(self.risk.kill_switch.reason)

    def trip_kill_switch(self, reason: str) -> None:
        if self.risk.kill_switch.trip(reason, self.clock.now()):
            self._on_kill(reason)

    def _on_kill(self, reason: str) -> None:
        for sid, slot in self.slots.items():
            if slot.state is StrategyState.RUNNING:
                self.stop_strategy(sid)
        self.square_off_all(f"kill switch: {reason}")
        self._alert(f"KILL SWITCH: {reason}. All strategies stopped, squaring off.", "critical")

    # --- recovery support -----------------------------------------------------------------

    def block(self, reason: str) -> None:
        """Keep every strategy stopped until a human resolves `reason` via the API."""
        self.blocked_reason = reason
        for sid, slot in self.slots.items():
            if slot.state is StrategyState.RUNNING:
                self.stop_strategy(sid)
        self._alert(f"ENGINE BLOCKED: {reason}. Strategies disabled until resolved.", "critical")

    def unblock(self) -> None:
        self.blocked_reason = ""
        self._alert("Engine block resolved by operator (strategies remain stopped)", "warning")

    def adopt_order(self, order: Order) -> None:
        """Restore an order from the journal and hand it to the executor."""
        self.orders[order.client_order_id] = order
        self.executor.adopt(order)
        self._pump()

    def apply_external_fill(self, order: Order, fill: Fill) -> None:
        """Book a fill that did not come from an executor (operator-adopted positions)."""
        self.orders[order.client_order_id] = order
        self.journal.record_order(order)
        self.fills.append(fill)
        self.positions.apply_fill(fill)
        self.journal.record_fill(fill)

    def daily_summary(self) -> dict[str, dict[str, float]]:
        """Per-strategy and account ('__account__') P&L for the day."""
        trades: dict[str, int] = {}
        for f in self.fills:
            trades[f.strategy_id] = trades.get(f.strategy_id, 0) + 1
        out: dict[str, dict[str, float]] = {}
        for sid, pnl in self.positions.pnl_by_strategy().items():
            out[sid] = {
                "gross": pnl.gross,
                "charges": pnl.charges,
                "net": pnl.net,
                "realized": pnl.realized,
                "unrealized": pnl.unrealized,
                "trades": float(trades.get(sid, 0)),
            }
        total = self.positions.pnl()
        out["__account__"] = {
            "gross": total.gross,
            "charges": total.charges,
            "net": total.net,
            "realized": total.realized,
            "unrealized": total.unrealized,
            "trades": float(len(self.fills)),
        }
        return out

    def reset_kill_switch(self) -> None:
        """Strategies stay stopped; a human starts them again."""
        self.risk.kill_switch.reset()
        self._alert("Kill switch reset (strategies remain stopped)", "warning")

    # --- event pump -----------------------------------------------------------------------------

    def _post(self, fn: Callable[[], None]) -> None:
        self._events.append(fn)

    def _pump(self) -> None:
        if self._pumping:
            return
        self._pumping = True
        try:
            while self._events:
                self._events.popleft()()
        finally:
            self._pumping = False

    def _handle_fill(self, fill: Fill) -> None:
        slot = self.slots.get(fill.strategy_id)
        if slot is not None:
            self._call(slot, slot.strategy.on_fill, fill)
        self._alert(
            f"Fill [{fill.strategy_id}] {fill.side} {fill.quantity} "
            f"{fill.instrument.tradingsymbol} @ {fill.price:.2f}",
            "info",
        )

    def _handle_order_update(self, order: Order) -> None:
        slot = self.slots.get(order.request.strategy_id)
        if slot is not None:
            self._call(slot, slot.strategy.on_order, order)

    # --- alerts -----------------------------------------------------------------------------------

    def _alert(self, text: str, level: Level) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self.notifier.send(text, level))
        except RuntimeError:
            return  # no running loop (sync unit tests)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)


class _ExecutionAdapter:
    """Binds executors to the engine without the FeedSink.on_order_update name clash."""

    def __init__(self, engine: TradingEngine) -> None:
        self._engine = engine

    def on_order_update(self, order: Order) -> None:
        self._engine._executor_order_update(order)

    def on_fill(self, fill: Fill) -> None:
        self._engine.on_fill(fill)

    def last_tick(self, token: int) -> Tick | None:
        return self._engine.last_tick(token)
