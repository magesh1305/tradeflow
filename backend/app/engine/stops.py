"""Exchange-side protective stops.

A strategy calls `protect(instrument, stop_price)`, usually from `on_fill`. The
manager keeps one SL (stop-limit) order at the broker for that position:
- quantity follows the position (modified when it changes);
- the trigger follows later `protect` calls (trailing);
- it is cancelled when the position is flat, or when the strategy exits or
  unprotects (strategy exits release the stop first, so they never race it).
SL rather than SL-M: the limit sits `buffer_pct` beyond the trigger, so it fills
in a fast market but never at an absurd price. The strategy's in-code stop stays
as a second layer.

If the broker rejects a stop, it alerts and stops managing that position (no
retry loop). A stop cancelled outside our control is re-placed at most 3 times.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.models import Instrument, Order, OrderRequest, OrderStatus, OrderType, Product, Side
from app.engine.positions import PositionKey

if TYPE_CHECKING:
    from app.engine.engine import TradingEngine

log = logging.getLogger(__name__)

MAX_REPLACEMENTS = 3


class StopManager:
    def __init__(self, engine: TradingEngine, buffer_pct: float) -> None:
        self._engine = engine
        self._buffer = buffer_pct / 100
        self._desired: dict[PositionKey, float] = {}
        self._orders: dict[PositionKey, str] = {}
        self._replacements: dict[PositionKey, int] = {}
        self._releasing: set[str] = set()

    # --- API ---------------------------------------------------------------------

    def protect(self, strategy_id: str, inst: Instrument, product: Product, trigger: float) -> None:
        key = (strategy_id, inst.token, product)
        self._desired[key] = inst.round_price(trigger)
        self.sync(key)

    def release(self, key: PositionKey) -> None:
        self._desired.pop(key, None)
        self._replacements.pop(key, None)
        cid = self._orders.pop(key, None)
        if cid is not None:
            order = self._engine.orders.get(cid)
            if order is not None and not order.status.is_terminal:
                self._releasing.add(cid)
                self._engine.cancel_order(cid)

    def release_all(self) -> None:
        for key in list(self._desired) + list(self._orders):
            self.release(key)

    def stop_for(self, key: PositionKey) -> Order | None:
        cid = self._orders.get(key)
        return self._engine.orders.get(cid) if cid else None

    def protected(self) -> dict[PositionKey, float]:
        return dict(self._desired)

    # --- engine hooks --------------------------------------------------------------

    def on_position_change(self, key: PositionKey) -> None:
        if key in self._desired:
            self.sync(key)

    def on_order_terminal(self, order: Order) -> None:
        if not order.request.protective:
            return
        cid = order.client_order_id
        key = next((k for k, v in self._orders.items() if v == cid), None)
        if cid in self._releasing:
            self._releasing.discard(cid)
            return
        if key is None:
            return
        del self._orders[key]
        if order.status is OrderStatus.REJECTED:
            self._desired.pop(key, None)
            self._engine.alert(
                f"Exchange stop REJECTED for {order.request.instrument.tradingsymbol}: "
                f"{order.status_message}. In-code stop remains active.",
                "critical",
            )
        elif order.status is OrderStatus.CANCELLED and key in self._desired:
            n = self._replacements.get(key, 0)
            if n < MAX_REPLACEMENTS:
                self._replacements[key] = n + 1
                self.sync(key)
            else:
                self._desired.pop(key, None)
                self._engine.alert(
                    f"Exchange stop for {order.request.instrument.tradingsymbol} keeps being "
                    "cancelled; no longer re-placing it.",
                    "critical",
                )
        # COMPLETE: the stop filled; the position change will clean up the rest.

    # --- core ------------------------------------------------------------------------------

    def sync(self, key: PositionKey) -> None:
        pos = self._engine.positions.get(*key)
        qty = abs(pos.quantity) if pos else 0
        if pos is None or qty == 0:
            self.release(key)
            return
        trigger = self._desired.get(key)
        if trigger is None:
            return
        inst = pos.instrument
        side = Side.SELL if pos.quantity > 0 else Side.BUY
        limit = inst.round_price(
            trigger * (1 - self._buffer if side is Side.SELL else 1 + self._buffer)
        )
        existing = self.stop_for(key)
        if existing is not None and not existing.status.is_terminal:
            if existing.request.side is not side:
                self.release(key)  # position flipped; protect again explicitly
                return
            remaining = existing.request.quantity - existing.filled_quantity
            if remaining != qty or existing.request.trigger_price != trigger:
                self._engine.executor.modify(
                    existing.client_order_id,
                    price=limit,
                    trigger_price=trigger,
                    quantity=existing.filled_quantity + qty,
                )
            return
        req = OrderRequest(
            strategy_id=key[0],
            instrument=inst,
            side=side,
            quantity=qty,
            order_type=OrderType.SL,
            product=key[2],
            price=limit,
            trigger_price=trigger,
            reason="protective stop",
            protective=True,
        )
        if not self._engine.risk.is_exit(req, self._engine.open_orders()):
            log.warning(
                "protective stop would not reduce the position; skipped",
                extra={"symbol": inst.tradingsymbol},
            )
            return
        order = self._engine.submit_order(req)
        if order.status is not OrderStatus.REJECTED:
            self._orders[key] = order.client_order_id
