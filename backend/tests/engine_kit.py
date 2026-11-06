"""Engine builders and a scripted executor for F&O tests."""

from __future__ import annotations

import asyncio
import itertools
from datetime import date
from typing import Any

from app.core.clock import ManualClock
from app.core.models import (
    Exchange,
    Fill,
    Instrument,
    InstrumentType,
    Order,
    OrderStatus,
    Tick,
)
from app.data.instruments import InstrumentStore
from app.engine.engine import EngineConfig, TradingEngine
from app.engine.positions import PositionBook
from app.execution.base import ExecutionListener
from app.execution.paper import PaperExecutor
from app.notify.base import LogNotifier
from app.risk.manager import RiskLimits, RiskManager
from app.risk.margin import MarginChecker
from app.storage.journal import Journal, MemoryJournalStore
from tests.builders import CE, FUT, ist, tick
from tests.test_engine import ManualFeed, Recorder

EXP = date(2025, 11, 11)
PE = Instrument(
    2002, "NIFTY25N1124000PE", Exchange.NFO, "NIFTY", InstrumentType.PE, 75, 0.05, EXP, 24000.0
)
CE_HI = Instrument(
    2003, "NIFTY25N1124200CE", Exchange.NFO, "NIFTY", InstrumentType.CE, 75, 0.05, EXP, 24200.0
)
FUT_FAR = Instrument(
    1002, "NIFTY25DECFUT", Exchange.NFO, "NIFTY", InstrumentType.FUT, 75, 0.1, date(2025, 12, 30)
)
STOCK_CE = Instrument(
    3001,
    "RELIANCE25NOV1500CE",
    Exchange.NFO,
    "RELIANCE",
    InstrumentType.CE,
    500,
    0.05,
    date(2025, 11, 25),
    1500.0,
)
ALL = [FUT, FUT_FAR, CE, PE, CE_HI, STOCK_CE]


def liquid(inst: Instrument, ltp: float, ts: Any = None, volume: int = 500_000) -> Tick:
    """A tick with a tight two-sided quote and plenty of volume."""
    half = inst.tick_size
    return tick(
        inst,
        ltp,
        ts or ist(10, 0),
        bid=round(ltp - half, 2),
        ask=round(ltp + half, 2),
        volume=volume,
    )


class ScriptedExecutor:
    """Accepts orders and leaves them OPEN; tests decide fills/rejections."""

    def __init__(self) -> None:
        self._listener: ExecutionListener | None = None
        self.orders: dict[str, Order] = {}
        self.submitted: list[Order] = []
        self.cancelled: list[str] = []
        self.modified: list[tuple[str, dict[str, Any]]] = []
        self._ids = itertools.count(1)

    @property
    def listener(self) -> ExecutionListener:
        assert self._listener is not None
        return self._listener

    def bind(self, listener: ExecutionListener) -> None:
        self._listener = listener

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def adopt(self, order: Order) -> None:
        self.orders[order.client_order_id] = order

    def on_tick(self, t: Tick) -> None: ...

    def submit(self, order: Order) -> None:
        order.broker_order_id = f"S{next(self._ids)}"
        order.status = (
            OrderStatus.TRIGGER_PENDING if order.request.trigger_price else OrderStatus.OPEN
        )
        self.orders[order.client_order_id] = order
        self.submitted.append(order)
        self.listener.on_order_update(order)

    def cancel(self, client_order_id: str) -> None:
        o = self.orders.get(client_order_id)
        if o is None or o.status.is_terminal:
            return
        self.cancelled.append(client_order_id)
        o.status = OrderStatus.CANCELLED
        self.listener.on_order_update(o)

    def modify(self, client_order_id: str, **kw: Any) -> None:
        self.modified.append((client_order_id, kw))
        o = self.orders[client_order_id]
        if kw.get("price") is not None:
            o.request.price = kw["price"]
        if kw.get("trigger_price") is not None:
            o.request.trigger_price = kw["trigger_price"]
        if kw.get("quantity") is not None:
            o.request.quantity = kw["quantity"]

    # --- test controls ---------------------------------------------------------------

    def fill(self, order: Order, price: float, qty: int | None = None) -> None:
        q = qty if qty is not None else order.request.quantity - order.filled_quantity
        new_total = order.filled_quantity + q
        order.average_price = (order.average_price * order.filled_quantity + price * q) / new_total
        order.filled_quantity = new_total
        if new_total >= order.request.quantity:
            order.status = OrderStatus.COMPLETE
        r = order.request
        self.listener.on_fill(
            Fill(
                f"{order.broker_order_id}-{new_total}",
                order.client_order_id,
                r.strategy_id,
                r.instrument,
                r.side,
                q,
                price,
                ist(10, 0),
                r.product,
                order.broker_order_id,
            )
        )
        self.listener.on_order_update(order)

    def reject(self, order: Order, message: str = "RMS rejected") -> None:
        order.status = OrderStatus.REJECTED
        order.status_message = message
        self.listener.on_order_update(order)


async def build(
    *,
    executor: Any = None,
    margin: MarginChecker | None = None,
    config: EngineConfig | None = None,
    h: int = 10,
    d: date = date(2025, 11, 3),
    journal: bool = False,
    **limits: Any,
) -> tuple[TradingEngine, Recorder, LogNotifier]:
    clock = ManualClock(ist(h, 0, d=d))
    store = InstrumentStore()
    store.load(ALL, d)
    positions = PositionBook()
    notifier = LogNotifier()
    j = None
    if journal:
        j = Journal(MemoryJournalStore(), "paper", notifier)
        await j.start()
    engine = TradingEngine(
        instruments=store,
        feed=ManualFeed(),
        executor=executor or PaperExecutor(clock, slippage_ticks=0),
        risk=RiskManager(RiskLimits(**limits), positions, clock),
        positions=positions,
        clock=clock,
        notifier=notifier,
        config=config or EngineConfig(),
        journal=j,
        margin=margin,
    )
    s = Recorder("s")
    engine.add_strategy(s)
    await engine.start(["s"])
    return engine, s, notifier


async def feed(engine: TradingEngine, *ticks: Tick) -> None:
    engine.on_ticks(list(ticks))
    await engine.drain()
    await asyncio.sleep(0)
