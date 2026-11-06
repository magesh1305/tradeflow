"""Simulated fills against live (or simulated) market data. Never calls a broker.

- MARKET: a buy fills at the ask (or the LTP when there is no ask) plus N ticks of
  slippage, and a sell fills at the bid (or the LTP) minus N ticks. An order
  submitted before any tick rests until the first tick arrives.
- LIMIT: fills when marketable. A buy fills when ask <= limit, or LTP <= limit
  when there is no ask, at the better of the limit and the quote.
- SL (stop-limit): triggers when LTP crosses the trigger (buy: LTP >= trigger;
  sell: LTP <= trigger), then behaves like a LIMIT order at `price`.
- SL-M: triggers the same way, then fills like a MARKET order.
Fills are always for the full quantity.
"""

from __future__ import annotations

import itertools
import logging

from app.core.clock import Clock
from app.core.models import Fill, Order, OrderStatus, OrderType, Side, Tick
from app.execution.base import ExecutionListener

log = logging.getLogger(__name__)


class PaperExecutor:
    def __init__(self, clock: Clock, slippage_ticks: int = 1) -> None:
        self._clock = clock
        self._slippage_ticks = slippage_ticks
        self._listener: ExecutionListener | None = None
        self._open: dict[str, Order] = {}
        self._triggered: set[str] = set()
        self._seq = itertools.count(1)

    def bind(self, listener: ExecutionListener) -> None:
        self._listener = listener

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def adopt(self, order: Order) -> None:
        """Paper fills don't survive a restart: a restored open paper order is cancelled."""
        if not order.status.is_terminal:
            order.status = OrderStatus.CANCELLED
            order.status_message = "cancelled on restart (paper orders do not survive restarts)"
            order.updated_at = self._clock.now()
            self.listener.on_order_update(order)

    @property
    def listener(self) -> ExecutionListener:
        if self._listener is None:
            raise RuntimeError("PaperExecutor is not bound to a listener")
        return self._listener

    # --- Executor interface ------------------------------------------------------

    def submit(self, order: Order) -> None:
        req = order.request
        order.broker_order_id = f"PAPER{next(self._seq):08d}"
        order.status = (
            OrderStatus.TRIGGER_PENDING
            if req.order_type in (OrderType.SL, OrderType.SL_M)
            else OrderStatus.OPEN
        )
        order.updated_at = self._clock.now()
        self._open[order.client_order_id] = order
        self.listener.on_order_update(order)
        tick = self.listener.last_tick(req.instrument.token)
        if tick is not None:
            self._try_fill(order, tick)

    def cancel(self, client_order_id: str) -> None:
        order = self._open.pop(client_order_id, None)
        if order is None:
            return
        self._triggered.discard(client_order_id)
        order.status = OrderStatus.CANCELLED
        order.updated_at = self._clock.now()
        self.listener.on_order_update(order)

    def modify(
        self,
        client_order_id: str,
        *,
        price: float | None = None,
        trigger_price: float | None = None,
        quantity: int | None = None,
    ) -> None:
        order = self._open.get(client_order_id)
        if order is None:
            return
        inst = order.request.instrument
        if price is not None:
            order.request.price = inst.round_price(price)
        if trigger_price is not None:
            order.request.trigger_price = inst.round_price(trigger_price)
        if quantity is not None and quantity > order.filled_quantity:
            order.request.quantity = quantity
        order.updated_at = self._clock.now()
        self.listener.on_order_update(order)
        tick = self.listener.last_tick(inst.token)
        if tick is not None:
            self._try_fill(order, tick)

    def on_tick(self, tick: Tick) -> None:
        for order in [o for o in self._open.values() if o.request.instrument.token == tick.token]:
            self._try_fill(order, tick)

    @property
    def open_orders(self) -> list[Order]:
        return list(self._open.values())

    # --- fill logic --------------------------------------------------------------

    def _market_price(self, order: Order, tick: Tick) -> float:
        inst = order.request.instrument
        slip = self._slippage_ticks * inst.tick_size
        if order.request.side is Side.BUY:
            base = tick.ask if tick.ask else tick.ltp
            return inst.round_price(base + slip)
        base = tick.bid if tick.bid else tick.ltp
        return max(inst.round_price(base - slip), inst.tick_size)

    @staticmethod
    def _limit_price(order: Order, tick: Tick) -> float | None:
        """Fill price if the limit is marketable on this tick, else None."""
        limit = order.request.price
        if limit is None:
            return None
        if order.request.side is Side.BUY:
            quote = tick.ask if tick.ask else tick.ltp
            return min(limit, quote) if quote <= limit else None
        quote = tick.bid if tick.bid else tick.ltp
        return max(limit, quote) if quote >= limit else None

    @staticmethod
    def _is_triggered(order: Order, tick: Tick) -> bool:
        trigger = order.request.trigger_price
        if trigger is None:
            return False
        if order.request.side is Side.BUY:
            return tick.ltp >= trigger
        return tick.ltp <= trigger

    def _try_fill(self, order: Order, tick: Tick) -> None:
        cid = order.client_order_id
        if cid not in self._open:
            return
        otype = order.request.order_type
        if otype in (OrderType.SL, OrderType.SL_M) and cid not in self._triggered:
            if not self._is_triggered(order, tick):
                return
            self._triggered.add(cid)
            order.status = OrderStatus.OPEN
            order.updated_at = tick.ts
            self.listener.on_order_update(order)

        if otype in (OrderType.MARKET, OrderType.SL_M):
            price: float | None = self._market_price(order, tick)
        else:
            price = self._limit_price(order, tick)
        if price is None:
            return
        self._fill(order, price, tick)

    def _fill(self, order: Order, price: float, tick: Tick) -> None:
        cid = order.client_order_id
        del self._open[cid]
        self._triggered.discard(cid)
        req = order.request
        fill = Fill(
            fill_id=f"{order.broker_order_id}-1",
            client_order_id=cid,
            strategy_id=req.strategy_id,
            instrument=req.instrument,
            side=req.side,
            quantity=req.quantity,
            price=price,
            ts=tick.ts,
            product=req.product,
            broker_order_id=order.broker_order_id,
        )
        order.filled_quantity = req.quantity
        order.average_price = price
        order.status = OrderStatus.COMPLETE
        order.updated_at = tick.ts
        self.listener.on_fill(fill)
        self.listener.on_order_update(order)
