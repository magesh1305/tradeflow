"""Multi-leg orders (spreads, straddles, rolls) handled as one group.

place:
  1. Every leg is risk-checked. If any leg fails, NO leg is sent (REJECTED).
  2. One basket margin check covers the opening legs (async in live mode).
  3. BUY legs are sent before SELL legs (hedge first, so Kite's margin benefit applies).
  4. WORKING until every leg is COMPLETE (FILLED).
failure (a leg REJECTED/CANCELLED by the broker, or not filled by the deadline):
  5. UNWINDING: cancel the legs still pending. Once a leg is terminal, exit
     whatever of it filled (never more than the strategy actually holds, so an
     unwind can't flip a position). Then UNWOUND, or UNWIND_FAILED with a
     critical alert if an unwind exit did not complete.
kill switch / square-off: open groups become ABORTED and do not unwind; the
square-off already flattens everything, and unwinding too would double-exit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.core.models import (
    Instrument,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Product,
    Side,
    new_client_order_id,
)

if TYPE_CHECKING:
    from app.engine.engine import TradingEngine

log = logging.getLogger(__name__)


class LegGroupStatus(StrEnum):
    PENDING = "PENDING"  # waiting for the margin check
    WORKING = "WORKING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    UNWINDING = "UNWINDING"
    UNWOUND = "UNWOUND"
    UNWIND_FAILED = "UNWIND_FAILED"
    ABORTED = "ABORTED"

    @property
    def is_open(self) -> bool:
        return self in (LegGroupStatus.PENDING, LegGroupStatus.WORKING, LegGroupStatus.UNWINDING)


@dataclass(frozen=True, slots=True)
class LegSpec:
    instrument: Instrument
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    product: Product | None = None


@dataclass
class LegGroup:
    id: str
    strategy_id: str
    legs: list[Order]
    status: LegGroupStatus
    created_at: datetime
    deadline: datetime | None = None
    error: str = ""
    unwind: dict[str, str | None] = field(default_factory=dict)  # leg cid -> unwind cid

    def as_row(self, mode: str, now: datetime) -> dict[str, Any]:
        return {
            "id": self.id,
            "trading_date": self.created_at.date(),
            "mode": mode,
            "strategy_id": self.strategy_id,
            "status": str(self.status),
            "legs": [
                {
                    "client_order_id": o.client_order_id,
                    "tradingsymbol": o.request.instrument.tradingsymbol,
                    "side": str(o.request.side),
                    "quantity": o.request.quantity,
                    "filled_quantity": o.filled_quantity,
                    "status": str(o.status),
                    "unwind_order": self.unwind.get(o.client_order_id),
                }
                for o in self.legs
            ],
            "error": self.error or None,
            "created_at": self.created_at,
            "updated_at": now,
        }


class LegGroupManager:
    def __init__(self, engine: TradingEngine, timeout_sec: float) -> None:
        self._engine = engine
        self._timeout = timedelta(seconds=timeout_sec)
        self.groups: dict[str, LegGroup] = {}

    # --- placement -------------------------------------------------------------------

    def place(
        self,
        strategy_id: str,
        specs: list[LegSpec],
        default_product: Product,
        timeout_sec: float | None = None,
    ) -> LegGroup:
        if not specs:
            raise ValueError("a leg group needs at least one leg")
        e = self._engine
        gid = new_client_order_id()
        now = e.clock.now()
        reqs = [
            OrderRequest(
                strategy_id=strategy_id,
                instrument=s.instrument,
                side=s.side,
                quantity=s.quantity,
                order_type=s.order_type,
                price=s.price,
                product=s.product or default_product,
                leg_group_id=gid,
                reason=f"leg {i + 1}/{len(specs)}",
            )
            for i, s in enumerate(specs)
        ]
        reqs.sort(key=lambda r: 0 if r.side is Side.BUY else 1)  # hedges first
        prepared = e.prepare_group(reqs)
        group = LegGroup(gid, strategy_id, [p.order for p in prepared], LegGroupStatus.PENDING, now)
        self.groups[gid] = group
        timeout = timedelta(seconds=timeout_sec) if timeout_sec else self._timeout

        rejected = [p for p in prepared if not p.decision.allowed]
        if rejected:
            reason = "; ".join(
                f"{p.order.request.instrument.tradingsymbol}: {p.decision.reason}" for p in rejected
            )
            for p in prepared:
                e.reject(
                    p.order,
                    p.decision.reason
                    if not p.decision.allowed
                    else f"leg group rejected ({reason})",
                )
            self._set(group, LegGroupStatus.REJECTED, reason)
            return group

        def send_all() -> None:
            if group.status is not LegGroupStatus.PENDING:
                return  # aborted (kill switch / square-off) while margin was checked
            group.deadline = e.clock.now() + timeout
            self._set(group, LegGroupStatus.WORKING)
            for p in prepared:
                e.send(p.order, is_exit=p.decision.is_exit, chase=p.converted)

        def margin_failed(reason: str) -> None:
            if group.status is not LegGroupStatus.PENDING:
                return
            for p in prepared:
                e.reject(p.order, reason)
            self._set(group, LegGroupStatus.REJECTED, reason)

        entries = [p.order.request for p in prepared if not p.decision.is_exit]
        e.check_margin(entries, on_ok=send_all, on_fail=margin_failed)
        return group

    # --- events -----------------------------------------------------------------------

    def on_order(self, order: Order) -> None:
        gid = order.request.leg_group_id
        group = self.groups.get(gid) if gid else None
        if group is None:
            # Might be an unwind exit for some group.
            for g in self.groups.values():
                if g.status is LegGroupStatus.UNWINDING and order.client_order_id in (
                    g.unwind.values()
                ):
                    self._evaluate(g)
            return
        self._evaluate(group)

    def on_time(self, now: datetime) -> None:
        for g in list(self.groups.values()):
            if g.status is LegGroupStatus.WORKING and g.deadline and now >= g.deadline:
                pending = [
                    o.request.instrument.tradingsymbol
                    for o in g.legs
                    if o.status is not OrderStatus.COMPLETE
                ]
                self._fail(
                    g,
                    f"not filled within {self._timeout.total_seconds():.0f}s: {', '.join(pending)}",
                )

    def abort_all(self, reason: str) -> None:
        for g in self.groups.values():
            if g.status.is_open:
                self.abort(g, reason)

    def abort(self, g: LegGroup, reason: str) -> None:
        if g.status.is_open:
            self._set(g, LegGroupStatus.ABORTED, reason)

    def open_groups(self) -> list[LegGroup]:
        return [g for g in self.groups.values() if g.status.is_open]

    # --- state machine ------------------------------------------------------------------

    def _evaluate(self, g: LegGroup) -> None:
        if g.status is LegGroupStatus.WORKING:
            bad = [o for o in g.legs if o.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED)]
            if bad:
                o = bad[0]
                self._fail(
                    g,
                    f"leg {o.request.instrument.tradingsymbol} {o.status}: "
                    f"{o.status_message or 'no reason given'}",
                )
            elif all(o.status is OrderStatus.COMPLETE for o in g.legs):
                self._set(g, LegGroupStatus.FILLED)
            return
        if g.status is not LegGroupStatus.UNWINDING:
            return
        e = self._engine
        for leg in g.legs:
            cid = leg.client_order_id
            if leg.status.is_terminal and cid not in g.unwind:
                exit_order = None
                if leg.filled_quantity > 0:
                    exit_order = e.exit_quantity(
                        leg.request.strategy_id,
                        leg.request.instrument,
                        leg.request.product,
                        opened_side=leg.request.side,
                        qty=leg.filled_quantity,
                        reason="leg group unwind",
                    )
                g.unwind[cid] = exit_order.client_order_id if exit_order else None
        if not all(o.status.is_terminal for o in g.legs):
            return
        unwinds = [e.orders[c] for c in g.unwind.values() if c is not None]
        if not all(o.status.is_terminal for o in unwinds):
            return
        failed = [o for o in unwinds if o.status is not OrderStatus.COMPLETE]
        if failed:
            self._set(
                g,
                LegGroupStatus.UNWIND_FAILED,
                g.error
                + "; unwind incomplete: "
                + ", ".join(f"{o.request.instrument.tradingsymbol} {o.status}" for o in failed),
            )
            e.alert(
                f"LEG GROUP UNWIND FAILED [{g.strategy_id}]: {g.error}. Check positions now.",
                "critical",
            )
        else:
            self._set(g, LegGroupStatus.UNWOUND)
            e.alert(f"Leg group [{g.strategy_id}] unwound after failure: {g.error}", "warning")

    def _fail(self, g: LegGroup, reason: str) -> None:
        self._set(g, LegGroupStatus.UNWINDING, reason)
        log.error("leg group failed; unwinding", extra={"group": g.id, "reason": reason})
        self._engine.alert(f"Leg group failed [{g.strategy_id}]: {reason}. Unwinding.", "critical")
        for o in g.legs:
            if not o.status.is_terminal:
                self._engine.cancel_order(o.client_order_id)
        self._evaluate(g)

    def _set(self, g: LegGroup, status: LegGroupStatus, error: str = "") -> None:
        g.status = status
        if error:
            g.error = error
        self._engine.on_leg_group_change(g)
