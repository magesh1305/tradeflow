"""Live execution through KiteBroker (real orders, real money).

Order lifecycle:
1. `submit` records the order locally (PENDING) and places it in a background
   task. The 16-char client order id goes to Kite as the `tag`.
2. An order above the underlying's freeze limit is placed with `autoslice=true`.
   Kite splits it into at most 10 child orders, each with its own order_id and
   `tag: null` (verified in the Kite docs). Children are matched by the order_ids
   in the placement response. Updates that arrive before that response are
   buffered and replayed. A normal order is simply an order with one child.
3. Fills: Kite reports cumulative `filled_quantity` / `average_price` per child.
   Each increase becomes a Fill with the incremental quantity and price:
       px = (avg_new * q_new - avg_old * q_old) / (q_new - q_old)
   Fill ids are `{child_order_id}-{cumulative_qty}`, so replays are idempotent.
   Our order's filled quantity and average price aggregate all its children.
4. Status: one child means Kite's status as-is. Several children: open until every
   child is terminal, then COMPLETE if fully filled, REJECTED if nothing filled
   and a slice was rejected, otherwise CANCELLED (a partial fill, kept in the message).
5. Every 20s `kite.orders()` is reconciled through the same update path. An order
   whose placement outcome was unknown and is not found within `unknown_timeout`
   is marked REJECTED.

Placement is never retried: a timeout may mean the order went through.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
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


@dataclass
class _Child:
    order_id: str
    status: OrderStatus = OrderStatus.OPEN
    filled: int = 0
    avg: float = 0.0
    message: str = ""


class LiveExecutor:
    def __init__(
        self,
        broker: KiteBroker,
        clock: Clock,
        *,
        freeze_limits: Mapping[str, int] | None = None,
        reconcile_interval: float = 20.0,
        unknown_timeout: float = 60.0,
        max_modifications: int = 25,
    ) -> None:
        self._broker = broker
        self._clock = clock
        self._freeze = dict(freeze_limits or {})
        self._reconcile_interval = reconcile_interval
        self._unknown_timeout = unknown_timeout
        self._max_modifications = max_modifications
        self._listener: ExecutionListener | None = None
        self._orders: dict[str, Order] = {}
        self._children: dict[str, dict[str, _Child]] = {}  # cid -> child order_id -> state
        self._by_broker: dict[str, str] = {}  # child order_id -> cid
        self._sliced: set[str] = set()
        self._submitted_at: dict[str, datetime] = {}
        self._cancel_requested: set[str] = set()
        self._modifications: dict[str, int] = {}
        self._orphans: deque[dict[str, Any]] = deque(maxlen=500)
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

    def _link(self, cid: str, child_id: str) -> None:
        self._children.setdefault(cid, {}).setdefault(child_id, _Child(child_id))
        self._by_broker[child_id] = cid
        order = self._orders[cid]
        if not order.broker_order_id:
            order.broker_order_id = child_id

    def needs_slicing(self, order: Order) -> bool:
        limit = self._freeze.get(order.request.instrument.name)
        return bool(limit) and order.request.quantity > limit  # type: ignore[operator]

    # --- Executor interface ------------------------------------------------------------

    def submit(self, order: Order) -> None:
        cid = order.client_order_id
        self._orders[cid] = order
        self._children.setdefault(cid, {})
        self._submitted_at[cid] = self._clock.now()
        self._spawn(self._place(order), f"place-{cid}")

    def adopt(self, order: Order) -> None:
        cid = order.client_order_id
        self._orders[cid] = order
        self._children.setdefault(cid, {})
        self._submitted_at[cid] = order.created_at
        if order.broker_order_id:
            self._link(cid, order.broker_order_id)
            child = self._children[cid][order.broker_order_id]
            child.filled, child.avg = order.filled_quantity, order.average_price
        elif not order.status.is_terminal:
            self.unknown.add(cid)

    def cancel(self, client_order_id: str) -> None:
        order = self._orders.get(client_order_id)
        if order is None or order.status.is_terminal:
            return
        open_children = [
            c.order_id
            for c in self._children.get(client_order_id, {}).values()
            if not c.status.is_terminal
        ]
        if not open_children:
            self._cancel_requested.add(client_order_id)  # cancelled once placed
            return
        for child_id in open_children:
            self._spawn(self._cancel(client_order_id, child_id), f"cancel-{child_id}")

    def modify(
        self,
        client_order_id: str,
        *,
        price: float | None = None,
        trigger_price: float | None = None,
        quantity: int | None = None,
    ) -> None:
        order = self._orders.get(client_order_id)
        children = self._children.get(client_order_id, {})
        if order is None or order.status.is_terminal or not children:
            return
        if quantity is not None and client_order_id in self._sliced:
            log.error(
                "cannot change the quantity of an auto-sliced order",
                extra={"client_order_id": client_order_id},
            )
            quantity = None
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
        for child in children.values():
            if not child.status.is_terminal:
                self._spawn(
                    self._modify(order, child.order_id, new_price, new_trigger, quantity),
                    f"modify-{child.order_id}",
                )

    def on_tick(self, tick: Any) -> None:
        pass  # the exchange fills live orders

    # --- broker calls -------------------------------------------------------------------

    async def _place(self, order: Order) -> None:
        cid = order.client_order_id
        sliced = self.needs_slicing(order)
        try:
            if sliced:
                resp = await self._broker.place_autoslice_order(order.request)
            else:
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

        if sliced:
            self._sliced.add(cid)
            raw = resp.get("children") if isinstance(resp, Mapping) else resp
            children = list(raw or [])
            errors: list[str] = []
            for i, child in enumerate(children):
                child_id = child.get("order_id") if isinstance(child, Mapping) else None
                if child_id:
                    self._link(cid, str(child_id))
                else:
                    err = child.get("error") if isinstance(child, Mapping) else child
                    msg = (
                        (err or {}).get("message", "slice failed")
                        if isinstance(err, Mapping)
                        else str(err)
                    )
                    errors.append(msg)
                    self._children[cid][f"ERR{i}"] = _Child(
                        f"ERR{i}", OrderStatus.REJECTED, message=msg
                    )
            if not children:
                self._finish(order, OrderStatus.REJECTED, "auto-slice returned no orders")
                return
            if errors:
                log.error(
                    "some auto-slices failed", extra={"client_order_id": cid, "errors": errors}
                )
        else:
            self._link(cid, broker_id)

        if order.status is OrderStatus.PENDING:
            order.status = OrderStatus.OPEN
            order.updated_at = self._clock.now()
            self.listener.on_order_update(order)
        self._replay_orphans()
        self._recompute(order)
        if cid in self._cancel_requested:
            self._cancel_requested.discard(cid)
            self.cancel(cid)

    async def _cancel(self, cid: str, child_id: str) -> None:
        try:
            await self._broker.cancel_order(child_id)
        except BrokerError as exc:
            # Often benign: the order completed before the cancel arrived.
            log.warning("cancel failed", extra={"client_order_id": cid, "error": str(exc)})

    async def _modify(
        self,
        order: Order,
        child_id: str,
        price: float | None,
        trigger: float | None,
        quantity: int | None,
    ) -> None:
        try:
            await self._broker.modify_order(
                child_id, price=price, trigger_price=trigger, quantity=quantity
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
        if quantity is not None:
            order.request.quantity = quantity
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

    def _replay_orphans(self) -> None:
        pending = [p for p in self._orphans if str(p.get("order_id")) in self._by_broker]
        for p in pending:
            self._orphans.remove(p)
            self.apply_update(p)

    def apply_update(self, p: dict[str, Any]) -> None:
        tag = p.get("tag")
        child_id = str(p.get("order_id") or "")
        cid = self._by_broker.get(child_id)
        if cid is None and tag in self._orders:
            cid = tag
            if child_id:
                self._link(cid, child_id)
        if cid is None:
            if child_id:
                self._orphans.append(p)  # maybe an auto-slice child we haven't linked yet
            return
        order = self._orders[cid]
        self.unknown.discard(cid)
        child = self._children[cid].setdefault(child_id, _Child(child_id))

        filled = int(p.get("filled_quantity") or 0)
        avg = float(p.get("average_price") or 0.0)
        fill: Fill | None = None
        if filled > child.filled:
            if avg <= 0:
                # Kite documents average_price for completed orders; wait for a later
                # update or the reconciliation rather than guess a price.
                log.warning("fill without average price; waiting", extra={"client_order_id": cid})
                return
            inc_q = filled - child.filled
            inc_px = (avg * filled - child.avg * child.filled) / inc_q
            if inc_px <= 0:
                inc_px = avg
            child.filled, child.avg = filled, avg
            req = order.request
            ts = (
                _parse_ts(p.get("exchange_update_timestamp"))
                or _parse_ts(p.get("exchange_timestamp"))
                or self._clock.now()
            )
            fill = Fill(
                fill_id=f"{child_id}-{filled}",
                client_order_id=cid,
                strategy_id=req.strategy_id,
                instrument=req.instrument,
                side=req.side,
                quantity=inc_q,
                price=round(inc_px, 4),
                ts=ts,
                product=req.product,
                broker_order_id=child_id,
            )
        status = map_status(p.get("status"))
        if status is not None and not child.status.is_terminal:
            child.status = status
        message = p.get("status_message")
        if message:
            child.message = str(message)
        if fill is not None:
            self.listener.on_fill(fill)
        self._recompute(order, force_notify=fill is not None)

    def _recompute(self, order: Order, *, force_notify: bool = False) -> None:
        cid = order.client_order_id
        children = list(self._children.get(cid, {}).values())
        if not children:
            return
        filled = sum(c.filled for c in children)
        value = sum(c.filled * c.avg for c in children)
        changed = force_notify
        if filled != order.filled_quantity:
            order.filled_quantity = filled
            order.average_price = value / filled if filled else 0.0
            changed = True
        if not order.status.is_terminal:
            new_status, message = self._aggregate_status(order, children)
            if new_status is not order.status:
                order.status = new_status
                changed = True
            if message and message != order.status_message:
                order.status_message = message
                changed = True
        if changed:
            order.updated_at = self._clock.now()
            if order.status.is_terminal:
                self._cancel_requested.discard(cid)
            self.listener.on_order_update(order)

    def _aggregate_status(self, order: Order, children: list[_Child]) -> tuple[OrderStatus, str]:
        message = next((c.message for c in reversed(children) if c.message), "")
        if len(children) == 1 and cid_not_sliced(order, self._sliced):
            return children[0].status, message
        if not all(c.status.is_terminal for c in children):
            if any(c.status is OrderStatus.OPEN for c in children if not c.status.is_terminal):
                return OrderStatus.OPEN, message
            return OrderStatus.TRIGGER_PENDING, message
        filled = sum(c.filled for c in children)
        if filled >= order.request.quantity:
            return OrderStatus.COMPLETE, message
        if filled == 0 and any(c.status is OrderStatus.REJECTED for c in children):
            return OrderStatus.REJECTED, message
        return OrderStatus.CANCELLED, (
            f"partially filled {filled}/{order.request.quantity}"
            + (f"; {message}" if message else "")
        )

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
            matched = [by_id[c] for c in self._children.get(cid, {}) if c in by_id]
            if not matched and cid in by_tag:
                matched = [by_tag[cid]]
            for row in matched:
                self.apply_update(row)
            if matched:
                continue
            age = (now - self._submitted_at.get(cid, now)).total_seconds()
            if age >= self._unknown_timeout and (
                cid in self.unknown or not self._children.get(cid)
            ):
                log.critical(
                    "order not found at Kite; marking rejected", extra={"client_order_id": cid}
                )
                self._finish(
                    order,
                    OrderStatus.REJECTED,
                    f"not found at Kite after {int(age)}s (placement failed)",
                )


def cid_not_sliced(order: Order, sliced: set[str]) -> bool:
    return order.client_order_id not in sliced
