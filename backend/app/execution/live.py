"""Live execution through KiteBroker (real orders, real money).

Order lifecycle:
1. `submit` records the order locally (PENDING) and places it in a background
   task. The 16-char client order id goes to Kite as the `tag`.
2. Kite order updates (WebSocket `on_order_update`, same payload as postbacks)
   are matched back by tag or by Kite order id.
3. Partial fills: Kite reports the cumulative `filled_quantity` and `average_price`.
   Each increase becomes a Fill with the incremental quantity and price:
       px = (avg_new * q_new - avg_old * q_old) / (q_new - q_old)
   Fill ids are `{order_id}-{cumulative_qty}`, so replays are idempotent.
4. Every 20s `kite.orders()` is reconciled through the same update path, which
   catches missed WebSocket messages and resolves placements whose outcome was
   unknown (timeouts). An order still not found at Kite after `unknown_timeout`
   is marked REJECTED and alerted.

Placement is never retried: a timeout may mean the order went through.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from datetime import datetime
from typing import Any

from app.brokers.kite import BrokerError, KiteBroker
from app.core.clock import Clock, to_ist
from app.core.models import Fill, Order, OrderStatus
from app.execution.base import ExecutionListener

log = logging.getLogger(__name__)

# Kite status -> our status. Statuses not listed here (including unknown future
# values) leave the order open.
_STATUS_MAP: dict[str, OrderStatus] = {
    "COMPLETE": OrderStatus.COMPLETE,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCEL": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "TRIGGER PENDING": OrderStatus.TRIGGER_PENDING,
    "OPEN": OrderStatus.OPEN,
}
_KNOWN_INTERIM = frozenset(
    {
        "UPDATE",
        "MODIFIED",
        "PUT ORDER REQ RECEIVED",
        "VALIDATION PENDING",
        "OPEN PENDING",
        "MODIFY VALIDATION PENDING",
        "MODIFY PENDING",
        "CANCEL PENDING",
        "AMO REQ RECEIVED",
    }
)


def map_status(raw: Any) -> OrderStatus | None:
    """None means 'still working, keep the current status'."""
    status = str(raw or "").upper()
    if status in _STATUS_MAP:
        return _STATUS_MAP[status]
    if status and status not in _KNOWN_INTERIM:
        log.warning("unknown Kite order status; treating as open", extra={"status": status})
    return None


def _parse_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return to_ist(raw)
    if isinstance(raw, str) and raw:
        try:
            return to_ist(datetime.fromisoformat(raw))
        except ValueError:
            return None
    return None


class LiveExecutor:
    def __init__(
        self,
        broker: KiteBroker,
        clock: Clock,
        *,
        reconcile_interval: float = 20.0,
        unknown_timeout: float = 60.0,
        max_modifications: int = 25,
    ) -> None:
        self._broker = broker
        self._clock = clock
        self._reconcile_interval = reconcile_interval
        self._unknown_timeout = unknown_timeout
        self._max_modifications = max_modifications
        self._listener: ExecutionListener | None = None
        self._orders: dict[str, Order] = {}
        self._by_broker: dict[str, str] = {}
        self._filled: dict[str, tuple[int, float]] = {}
        self._submitted_at: dict[str, datetime] = {}
        self._cancel_requested: set[str] = set()
        self._modifications: dict[str, int] = {}
        self.unknown: set[str] = set()  # placement outcome unknown
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loop_task: asyncio.Task[None] | None = None

    # --- wiring --------------------------------------------------------------------

    def bind(self, listener: ExecutionListener) -> None:
        self._listener = listener

    @property
    def listener(self) -> ExecutionListener:
        if self._listener is None:
            raise RuntimeError("LiveExecutor is not bound to a listener")
        return self._listener

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._reconcile_loop(), name="live-reconcile")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5)

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _link(self, cid: str, broker_order_id: str) -> None:
        self._orders[cid].broker_order_id = broker_order_id
        self._by_broker[broker_order_id] = cid

    # --- Executor interface ------------------------------------------------------------

    def submit(self, order: Order) -> None:
        cid = order.client_order_id
        self._orders[cid] = order
        self._submitted_at[cid] = self._clock.now()
        self._spawn(self._place(order), f"place-{cid}")

    def adopt(self, order: Order) -> None:
        cid = order.client_order_id
        self._orders[cid] = order
        self._submitted_at[cid] = order.created_at
        self._filled[cid] = (order.filled_quantity, order.average_price)
        if order.broker_order_id:
            self._by_broker[order.broker_order_id] = cid
        elif not order.status.is_terminal:
            self.unknown.add(cid)

    def cancel(self, client_order_id: str) -> None:
        order = self._orders.get(client_order_id)
        if order is None or order.status.is_terminal:
            return
        if not order.broker_order_id:
            self._cancel_requested.add(client_order_id)  # cancelled once placed
            return
        self._spawn(self._cancel(order), f"cancel-{client_order_id}")

    def modify(
        self,
        client_order_id: str,
        *,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> None:
        order = self._orders.get(client_order_id)
        if order is None or order.status.is_terminal or not order.broker_order_id:
            return
        count = self._modifications.get(client_order_id, 0)
        if count >= self._max_modifications:
            log.error(
                "Kite allows at most %d modifications per order; cancel and re-place instead",
                self._max_modifications,
                extra={"client_order_id": client_order_id},
            )
            return
        self._modifications[client_order_id] = count + 1
        inst = order.request.instrument
        new_price = inst.round_price(price) if price is not None else None
        new_trigger = inst.round_price(trigger_price) if trigger_price is not None else None
        self._spawn(self._modify(order, new_price, new_trigger), f"modify-{client_order_id}")

    def on_tick(self, tick: Any) -> None:
        pass  # the exchange fills live orders

    # --- broker calls -------------------------------------------------------------------

    async def _place(self, order: Order) -> None:
        cid = order.client_order_id
        try:
            broker_id = await self._broker.place_order(order.request)
        except BrokerError as exc:
            if exc.outcome_unknown:
                self.unknown.add(cid)
                order.status_message = f"placement outcome unknown ({exc}); reconciling"
                log.critical(
                    "order placement outcome unknown",
                    extra={"client_order_id": cid, "error": str(exc)},
                )
                self.listener.on_order_update(order)
            else:
                self._finish(order, OrderStatus.REJECTED, f"broker: {exc}")
            return
        if not order.broker_order_id:
            self._link(cid, broker_id)
        if order.status is OrderStatus.PENDING:
            order.status = OrderStatus.OPEN
            order.updated_at = self._clock.now()
            self.listener.on_order_update(order)
        if cid in self._cancel_requested:
            self._cancel_requested.discard(cid)
            await self._cancel(order)

    async def _cancel(self, order: Order) -> None:
        assert order.broker_order_id is not None  # noqa: S101 - checked by callers
        try:
            await self._broker.cancel_order(order.broker_order_id)
        except BrokerError as exc:
            # Often benign: the order completed before the cancel arrived.
            log.warning(
                "cancel failed",
                extra={"client_order_id": order.client_order_id, "error": str(exc)},
            )

    async def _modify(self, order: Order, price: float | None, trigger: float | None) -> None:
        assert order.broker_order_id is not None  # noqa: S101 - checked by caller
        try:
            await self._broker.modify_order(
                order.broker_order_id, price=price, trigger_price=trigger
            )
        except BrokerError as exc:
            log.warning(
                "modify failed",
                extra={"client_order_id": order.client_order_id, "error": str(exc)},
            )
            return
        if price is not None:
            order.request.price = price
        if trigger is not None:
            order.request.trigger_price = trigger
        order.updated_at = self._clock.now()
        self.listener.on_order_update(order)

    def _finish(self, order: Order, status: OrderStatus, message: str) -> None:
        order.status = status
        order.status_message = message
        order.updated_at = self._clock.now()
        self.unknown.discard(order.client_order_id)
        self._cancel_requested.discard(order.client_order_id)
        self.listener.on_order_update(order)

    # --- order updates --------------------------------------------------------------------

    def on_broker_update(self, payload: dict[str, Any]) -> None:
        """Kite WebSocket order update (also used for reconciliation rows)."""
        self.apply_update(payload)

    def apply_update(self, p: dict[str, Any]) -> None:
        tag = p.get("tag")
        broker_id = str(p.get("order_id") or "")
        cid = tag if tag in self._orders else self._by_broker.get(broker_id)
        if cid is None:
            log.debug("order update for an order we did not place", extra={"order_id": broker_id})
            return
        order = self._orders[cid]
        if broker_id and not order.broker_order_id:
            self._link(cid, broker_id)
        self.unknown.discard(cid)

        filled = int(p.get("filled_quantity") or 0)
        avg = float(p.get("average_price") or 0.0)
        prev_q, prev_avg = self._filled.get(cid, (0, 0.0))
        fill: Fill | None = None
        if filled > prev_q:
            if avg <= 0:
                # Kite documents average_price for completed orders; wait for a later
                # update or the reconciliation rather than guess a price.
                log.warning("fill without average price; waiting", extra={"client_order_id": cid})
                return
            inc_q = filled - prev_q
            inc_px = (avg * filled - prev_avg * prev_q) / inc_q
            if inc_px <= 0:
                inc_px = avg
            self._filled[cid] = (filled, avg)
            req = order.request
            ts = (
                _parse_ts(p.get("exchange_update_timestamp"))
                or _parse_ts(p.get("exchange_timestamp"))
                or self._clock.now()
            )
            fill = Fill(
                fill_id=f"{order.broker_order_id}-{filled}",
                client_order_id=cid,
                strategy_id=req.strategy_id,
                instrument=req.instrument,
                side=req.side,
                quantity=inc_q,
                price=round(inc_px, 4),
                ts=ts,
                product=req.product,
                broker_order_id=order.broker_order_id,
            )
            order.filled_quantity = filled
            order.average_price = avg

        changed = False
        if not order.status.is_terminal:
            status = map_status(p.get("status"))
            if status is not None and status is not order.status:
                order.status = status
                changed = True
            elif status is None and order.status is OrderStatus.PENDING:
                order.status = OrderStatus.OPEN
                changed = True
            message = p.get("status_message")
            if message and message != order.status_message:
                order.status_message = str(message)
                changed = True
        if fill is not None:
            self.listener.on_fill(fill)
        if changed or fill is not None:
            order.updated_at = self._clock.now()
            if order.status.is_terminal:
                self._cancel_requested.discard(cid)
            self.listener.on_order_update(order)

    # --- reconciliation ----------------------------------------------------------------------

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reconcile_interval)
            try:
                await self.reconcile()
            except Exception:
                log.exception("order reconciliation failed")

    async def reconcile(self) -> None:
        open_orders = [o for o in self._orders.values() if not o.status.is_terminal]
        if not open_orders:
            return
        rows = await self._broker.orders()
        by_id = {str(r.get("order_id")): r for r in rows}
        by_tag = {r["tag"]: r for r in rows if r.get("tag")}
        now = self._clock.now()
        for order in open_orders:
            cid = order.client_order_id
            row = by_id.get(order.broker_order_id or "") or by_tag.get(cid)
            if row is not None:
                self.apply_update(row)
                continue
            age = (now - self._submitted_at.get(cid, now)).total_seconds()
            if age >= self._unknown_timeout and (cid in self.unknown or not order.broker_order_id):
                log.critical(
                    "order not found at Kite; marking rejected", extra={"client_order_id": cid}
                )
                self._finish(
                    order,
                    OrderStatus.REJECTED,
                    f"not found at Kite after {int(age)}s (placement failed)",
                )
