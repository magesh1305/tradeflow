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
    Instrument,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Product,
    Side,
    Tick,
)
from app.data.instruments import InstrumentStore, weekdays_until
from app.data.option_chain import OptionChainService
from app.engine.candles import CandleAggregator
from app.engine.charges import NoCharges
from app.engine.legs import LegGroup, LegGroupManager, LegSpec
from app.engine.positions import Position, PositionBook, PositionKey
from app.engine.smart_orders import Chaser, OptionOrderRules
from app.engine.smart_orders import prepare as prepare_option_order
from app.engine.stops import StopManager
from app.execution.base import Executor
from app.feeds.base import Feed
from app.notify.base import Level, Notifier
from app.risk.manager import RiskDecision, RiskManager
from app.risk.margin import MarginChecker
from app.strategies.base import Strategy

log = logging.getLogger(__name__)

SYSTEM = "__system__"


class ChargeModel(Protocol):
    def for_fill(self, fill: Fill) -> float: ...


class OrderJournal(Protocol):
    def record_order(self, order: Order) -> None: ...
    def record_fill(self, fill: Fill, charges: float = 0.0) -> None: ...
    def record_leg_group(self, group: LegGroup) -> None: ...


class NullJournal:
    def record_order(self, order: Order) -> None:
        pass

    def record_fill(self, fill: Fill, charges: float = 0.0) -> None:
        pass

    def record_leg_group(self, group: LegGroup) -> None:
        pass


@dataclass(frozen=True, slots=True)
class Prepared:
    order: Order
    decision: RiskDecision
    converted: bool  # option MARKET request turned into a chased LIMIT


class EngineBlockedError(RuntimeError):
    pass


class StrategyState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    ERROR = "ERROR"


class StrategyNotRunningError(RuntimeError):
    pass


class StrategyRunningError(RuntimeError):
    pass


class PlanRequiredError(RuntimeError):
    """No strategy may start until today's plan is submitted (Settings tab)."""


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
    option_rules: OptionOrderRules = field(default_factory=OptionOrderRules)
    stop_limit_buffer_pct: float = 5.0
    leg_group_timeout_sec: float = 30.0
    risk_free_rate: float = 0.065
    index_underlyings: frozenset[str] = frozenset(
        {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
    )
    physical_settlement_exit_days: int = 1
    physical_settlement_exit_time: time = time(15, 0)
    # Live/paper runtime: strategies can't start until today's plan is submitted.
    # Backtests don't use it.
    require_daily_plan: bool = False
    # Margin estimate (fraction of notional) for per-strategy capital limits.
    capital_margin_pct: float = 0.15


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
            self._engine.cancel_order(client_order_id)

    @property
    def option_chain(self) -> OptionChainService:
        return self._engine.option_chain

    def _require_running(self) -> None:
        if self._slot.state is not StrategyState.RUNNING:
            raise StrategyNotRunningError(f"{self.strategy_id} is {self._slot.state}")

    def place_legs(
        self, specs: list[LegSpec], product: Product, timeout_sec: float | None = None
    ) -> LegGroup:
        self._require_running()
        return self._engine.legs.place(self.strategy_id, specs, product, timeout_sec)

    def protect(self, instrument: Instrument, stop_price: float, product: Product) -> None:
        self._require_running()
        self._engine.stops.protect(self.strategy_id, instrument, product, stop_price)

    def unprotect(self, instrument: Instrument) -> None:
        for p in Product:
            self._engine.stops.release((self.strategy_id, instrument.token, p))


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
        margin: MarginChecker | None = None,
        charges: ChargeModel | None = None,
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
        self.margin = margin
        self.charges: ChargeModel = charges or NoCharges()
        self.fill_charges: dict[str, float] = {}
        self._unsent: set[str] = set()  # created, waiting for margin / not yet sent
        self.option_chain = OptionChainService(
            instruments, self.last_tick, clock, self.config.risk_free_rate
        )
        self.chaser = Chaser(
            self.config.option_rules,
            self.last_tick,
            lambda cid, px: self.executor.modify(cid, price=px),
            lambda text: self.alert(text, "warning"),
        )
        self.stops = StopManager(self, self.config.stop_limit_buffer_pct)
        self.legs = LegGroupManager(self, self.config.leg_group_timeout_sec)
        self._settlement_exited: set[tuple[PositionKey, date]] = set()
        self.plan_date: date | None = None  # trading date of the submitted daily plan
        self._alerted_targets: set[tuple[date, str]] = set()
        self.risk.estimator = self.estimate_margin
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
        elif not self.plan_submitted:
            log.warning("today's plan not submitted; strategies wait for it (Settings tab)")
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
        cost = self.charges.for_fill(fill)
        self.fill_charges[fill.fill_id] = cost
        self.positions.apply_fill(fill, cost)
        self.journal.record_fill(fill, cost)
        key = (fill.strategy_id, fill.instrument.token, fill.product)
        self._post(lambda: self._handle_fill(fill))
        self._post(lambda: self.stops.on_position_change(key))
        self._pump_if_idle()

    def _executor_order_update(self, order: Order) -> None:
        snapshot = dataclasses.replace(order)
        self.journal.record_order(snapshot)
        self._post(lambda: self._handle_order_update(snapshot))
        live = self.orders.get(order.client_order_id, order)
        if snapshot.status.is_terminal:
            self._post(lambda: self.stops.on_order_terminal(snapshot))
        if live.request.leg_group_id or snapshot.status.is_terminal:
            self._post(lambda: self.legs.on_order(live))
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
        self.chaser.on_time(now, self.orders)
        self.legs.on_time(now)
        self._physical_settlement_exits(now)
        self._pump()
        if now.time() >= self.config.square_off_time:
            self.scheduled_square_off()

    def _physical_settlement_exits(self, now: datetime) -> None:
        """Exit stock F&O positions before expiry to avoid physical delivery."""
        cfg = self.config
        if now.time() < cfg.physical_settlement_exit_time:
            return
        today = now.date()
        for pos in self.positions.open_positions():
            inst = pos.instrument
            if inst.expiry is None or inst.name in cfg.index_underlyings:
                continue
            if weekdays_until(today, inst.expiry) > cfg.physical_settlement_exit_days:
                continue
            marker = (pos.key, today)
            if marker in self._settlement_exited:
                continue
            self._settlement_exited.add(marker)
            self.stops.release(pos.key)
            self._exit(pos, "physical settlement exit before expiry")
            self.alert(
                f"Exiting {inst.tradingsymbol} before expiry (physical settlement)", "warning"
            )

    def advance_to(self, now: datetime) -> None:
        """Move market time forward (never back) and run time-based actions."""
        if now > self.clock.now():
            self.clock.set(now)
        self._on_time(self.clock.now())

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
                if o.request.strategy_id == slot.strategy.id and not o.request.protective:
                    self.cancel_order(o.client_order_id)
            self._alert(
                f"Strategy {slot.strategy.id} disabled after error: {slot.error}", "critical"
            )
        finally:
            self._hook_depth -= 1

    def start_strategy(self, strategy_id: str) -> None:
        slot = self.slots[strategy_id]
        if self.blocked_reason:
            raise EngineBlockedError(f"engine blocked: {self.blocked_reason}")
        if not self.plan_submitted:
            raise PlanRequiredError("submit today's plan in the Settings tab first")
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
        # Protective stops stay at the broker unless the positions are exited too.
        for o in self.open_orders():
            if o.request.strategy_id == strategy_id and not o.request.protective:
                self.cancel_order(o.client_order_id)
        if exit_positions:
            for pos in self.positions.open_positions(strategy_id):
                self.stops.release(pos.key)
                self._exit(pos, "strategy stopped")
        self._pump()
        log.info("strategy stopped", extra={"strategy": strategy_id, "exit": exit_positions})

    def subscribe_ticks(self, slot: StrategySlot, tokens: list[int]) -> None:
        slot.tick_tokens.update(tokens)
        self.feed.subscribe(tokens)

    def subscribe_market(self, tokens: list[int]) -> None:
        """Market data not owned by a strategy (e.g. the dashboard's option chain)."""
        self.feed.subscribe(tokens)

    def set_strategy_params(self, strategy_id: str, params: dict[str, Any]) -> Any:
        """Validate and apply new params. Only allowed while the strategy is stopped."""
        slot = self.slots[strategy_id]
        if slot.state is StrategyState.RUNNING:
            raise StrategyRunningError("stop the strategy before changing its params")
        validated = slot.strategy.Params.model_validate(params)
        slot.strategy.params = validated
        return validated

    def subscribe_candles(self, slot: StrategySlot, token: int, interval: int) -> None:
        self.candles.add(token, interval)
        slot.candle_subs.add((token, interval))
        self.feed.subscribe([token])

    # --- orders -------------------------------------------------------------------------

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.status.is_terminal]

    def submit_order(self, req: OrderRequest) -> Order:
        """The only path to an executor: risk check, option pricing, margin, then send."""
        (p,) = self.prepare_group([req])
        if not p.decision.allowed:
            self.reject(p.order, f"risk: {p.decision.reason}")
            return p.order
        if p.decision.is_exit or req.protective:
            self.send(p.order, is_exit=p.decision.is_exit, chase=p.converted)
            return p.order
        self.check_margin(
            [p.order.request],
            on_ok=lambda: self.send(p.order, is_exit=False, chase=p.converted),
            on_fail=lambda reason: self.reject(p.order, reason),
        )
        return p.order

    def prepare_group(self, reqs: list[OrderRequest]) -> list[Prepared]:
        """Risk-check each request and create its Order (not yet sent).

        Each request is checked BEFORE its own order is recorded: otherwise the new
        order would count as its own "pending exit" and a genuine exit would be
        misclassified as an entry. Later requests see earlier ones as pending."""
        pending = self.open_orders()
        now = self.clock.now()
        out: list[Prepared] = []
        for original in reqs:
            quote = self.last_tick(original.instrument.token)
            decision = self.risk.check(original, pending, quote)
            req, converted = original, False
            if decision.allowed:
                req, converted = prepare_option_order(
                    original, quote, is_exit=decision.is_exit, rules=self.config.option_rules
                )
            order = Order(req, created_at=now, updated_at=now)
            self.orders[order.client_order_id] = order
            self._unsent.add(order.client_order_id)
            pending.append(order)
            out.append(Prepared(order, decision, converted))
        return out

    def send(self, order: Order, *, is_exit: bool, chase: bool) -> None:
        cid = order.client_order_id
        if cid not in self._unsent or order.status.is_terminal:
            return  # cancelled while waiting for margin
        self._unsent.discard(cid)
        # Journal the intent (with its tag) before the broker sees it, so crash
        # recovery can match whatever Kite ends up holding.
        self.journal.record_order(order)
        if chase:
            self.chaser.track(order, is_exit=is_exit, now=self.clock.now())
        self.executor.submit(order)

    def reject(self, order: Order, reason: str) -> None:
        self._unsent.discard(order.client_order_id)
        order.status = OrderStatus.REJECTED
        order.status_message = reason
        order.updated_at = self.clock.now()
        self._executor_order_update(order)
        req = order.request
        self._alert(
            f"Order rejected [{req.strategy_id}] {req.side} {req.quantity} "
            f"{req.instrument.tradingsymbol}: {reason}",
            "warning",
        )

    def cancel_order(self, client_order_id: str) -> None:
        order = self.orders.get(client_order_id)
        if order is None or order.status.is_terminal:
            return
        if client_order_id in self._unsent:
            self._unsent.discard(client_order_id)
            order.status = OrderStatus.CANCELLED
            order.status_message = "cancelled before sending"
            order.updated_at = self.clock.now()
            self._executor_order_update(order)
            return
        self.executor.cancel(client_order_id)

    def check_margin(
        self,
        reqs: list[OrderRequest],
        *,
        on_ok: Callable[[], None],
        on_fail: Callable[[str], None],
    ) -> None:
        """Run `on_ok` if margin suffices for `reqs` (opening orders), else `on_fail`."""
        if not reqs or self.margin is None:
            on_ok()
            return
        mine = {r.client_order_id for r in reqs}
        pending = [o for o in self.open_orders() if o.client_order_id not in mine]
        result = self.margin.check_now(reqs, self.positions.open_positions(), pending)
        if result is not None:
            if result.ok:
                on_ok()
            else:
                on_fail(result.reason)
            return
        checker = self.margin

        async def run() -> None:
            res = await checker.check(reqs)
            if res.ok:
                on_ok()
            else:
                on_fail(res.reason)
            self._pump()

        task = asyncio.get_running_loop().create_task(run(), name="margin-check")
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def exit_quantity(
        self,
        strategy_id: str,
        inst: Instrument,
        product: Product,
        *,
        opened_side: Side,
        qty: int,
        reason: str,
    ) -> Order | None:
        """Exit up to `qty` of a position opened with `opened_side`, never more than held."""
        pos = self.positions.get(strategy_id, inst.token, product)
        if pos is None or pos.quantity == 0 or (pos.quantity > 0) != (opened_side is Side.BUY):
            return None
        side = opened_side.opposite
        in_flight = sum(
            o.pending_quantity
            for o in self.open_orders()
            if o.request.strategy_id == strategy_id
            and o.request.instrument.token == inst.token
            and o.request.product == product
            and o.request.side == side
            and not o.request.protective
        )
        qty = min(qty, abs(pos.quantity) - in_flight)
        if qty <= 0:
            return None
        self.stops.release(pos.key)
        return self.submit_order(
            OrderRequest(
                strategy_id=strategy_id,
                instrument=inst,
                side=side,
                quantity=qty,
                order_type=OrderType.MARKET,
                product=product,
                reason=reason,
            )
        )

    def _exit(self, pos: Position, reason: str) -> Order | None:
        side = Side.SELL if pos.quantity > 0 else Side.BUY
        in_flight = sum(
            o.pending_quantity
            for o in self.open_orders()
            if o.request.strategy_id == pos.strategy_id
            and o.request.instrument.token == pos.instrument.token
            and o.request.product == pos.product
            and o.request.side == side
            and not o.request.protective
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
        # Open leg groups are aborted (not unwound) and stops released: the
        # square-off below flattens everything, and unwinding too would double-exit.
        for g in self.legs.open_groups():
            if products is None or any(o.request.product in products for o in g.legs):
                self.legs.abort(g, reason)
        for key in list(self.stops.protected()):
            if products is None or key[2] in products:
                self.stops.release(key)
        for o in self.open_orders():
            if products is None or o.request.product in products:
                self.cancel_order(o.client_order_id)
        self._pump()
        for pos in self.positions.open_positions():
            if products is None or pos.product in products:
                self._exit(pos, reason)
        self._pump()
        log.warning("square-off", extra={"reason": reason})
        self._alert(f"Square-off: {reason}", "warning")

    # --- kill switch & P&L --------------------------------------------------------------------

    @property
    def plan_submitted(self) -> bool:
        return not self.config.require_daily_plan or self.plan_date == self.clock.now().date()

    def estimate_margin(self, inst: Instrument, side: Side, qty: int, price: float | None) -> float:
        """Rough margin: long options their premium, futures and short options a
        fraction of notional (strike for options)."""
        tick = self.last_tick(inst.token)
        px = price or (tick.ltp if tick else 0.0)
        pct = self.config.capital_margin_pct
        if inst.is_option:
            return px * qty if side is Side.BUY else (inst.strike or px) * qty * pct
        return px * qty * pct

    def check_pnl(self) -> None:
        pnl = self.positions.pnl()
        if self.risk.check_daily_loss(pnl.net):
            self._on_kill(self.risk.kill_switch.reason)
            return
        # Per-strategy loss limits: stop that strategy and exit its positions.
        for sid, net, limit in self.risk.strategies_over_loss_limit():
            slot = self.slots.get(sid)
            if slot is not None and slot.state is StrategyState.RUNNING:
                self.stop_strategy(sid, exit_positions=True)
                slot.error = f"loss limit {limit:,.0f} hit (net {net:,.2f})"
                self._alert(
                    f"{sid} loss limit hit: net {net:,.2f} <= -{limit:,.0f}. "
                    "Stopped and exiting its positions.",
                    "critical",
                )
        # Profit targets: no new trades (enforced by the RiskManager); alert once a day.
        today = self.clock.now().date()
        for who in self.risk.profit_targets_reached():
            if (today, who) not in self._alerted_targets:
                self._alerted_targets.add((today, who))
                self._alert(
                    f"Profit target reached ({who}): no new trades today; "
                    "open positions keep running with their stops and exits.",
                    "warning",
                )

    def trip_kill_switch(self, reason: str) -> None:
        if self.risk.kill_switch.trip(reason, self.clock.now()):
            self._on_kill(reason)

    def _on_kill(self, reason: str) -> None:
        # Abort open leg groups FIRST: stopping strategies cancels their leg orders,
        # which would otherwise start an unwind on top of the square-off (double exit).
        self.legs.abort_all(f"kill switch: {reason}")
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

    # --- leg groups & alerts -----------------------------------------------------------------

    def on_leg_group_change(self, group: LegGroup) -> None:
        self.journal.record_leg_group(group)
        slot = self.slots.get(group.strategy_id)
        if slot is not None:
            self._post(lambda: self._call(slot, slot.strategy.on_legs, group))

    def alert(self, text: str, level: Level = "info") -> None:
        self._alert(text, level)

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
