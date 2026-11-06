"""Pre-trade risk checks and the kill switch. Every order passes through `check()`.

Exit (position-reducing) orders are ALWAYS allowed, even when the kill switch is
tripped or outside the trading window, so the system can always get flat.

An order counts as an exit only when it reduces an existing position of the same
strategy, instrument and product, and its quantity fits in what is still
unhedged after other pending exit orders. Anything else (an opening order, an
add, the excess part of a flip) gets the full checks.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.config import Settings
from app.core.clock import Clock
from app.core.models import Order, OrderRequest
from app.engine.positions import PositionBook

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_lots_per_order: int = 10
    max_open_positions: int = 6
    max_orders_per_minute: int = 30
    daily_loss_limit: float = 10_000.0
    duplicate_window_sec: float = 2.0
    trading_start: time = time(9, 15)
    trading_end: time = time(15, 10)

    @classmethod
    def from_settings(cls, s: Settings) -> RiskLimits:
        return cls(
            max_lots_per_order=s.max_lots_per_order,
            max_open_positions=s.max_open_positions,
            max_orders_per_minute=s.max_orders_per_minute,
            daily_loss_limit=s.daily_loss_limit,
            duplicate_window_sec=s.duplicate_order_window_sec,
            trading_start=s.trading_start,
            trading_end=s.trading_end,
        )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    is_exit: bool
    reason: str = ""


class KillSwitch:
    def __init__(self) -> None:
        self.tripped = False
        self.reason = ""
        self.tripped_at: datetime | None = None

    def trip(self, reason: str, at: datetime) -> bool:
        """Returns True if this call tripped it (False if already tripped)."""
        if self.tripped:
            return False
        self.tripped, self.reason, self.tripped_at = True, reason, at
        log.critical("KILL SWITCH TRIPPED", extra={"reason": reason})
        return True

    def reset(self) -> None:
        self.tripped, self.reason, self.tripped_at = False, "", None


class RiskManager:
    def __init__(self, limits: RiskLimits, positions: PositionBook, clock: Clock) -> None:
        self.limits = limits
        self.kill_switch = KillSwitch()
        self._positions = positions
        self._clock = clock
        self._recent: deque[tuple[datetime, OrderRequest]] = deque()  # accepted entries
        self.rejections = 0

    # --- classification ------------------------------------------------------

    def is_exit(self, req: OrderRequest, pending: Iterable[Order]) -> bool:
        pos = self._positions.get(req.strategy_id, req.instrument.token, req.product)
        if pos is None or pos.quantity == 0:
            return False
        if (pos.quantity > 0) == (req.side.sign > 0):
            return False  # same direction: an add, not an exit
        already_exiting = sum(
            o.pending_quantity
            for o in pending
            if o.request.strategy_id == req.strategy_id
            and o.request.instrument.token == req.instrument.token
            and o.request.product == req.product
            and o.request.side == req.side
        )
        return req.quantity <= abs(pos.quantity) - already_exiting

    # --- checks ----------------------------------------------------------------

    def check(self, req: OrderRequest, pending: Iterable[Order] = ()) -> RiskDecision:
        pending = list(pending)
        if self.is_exit(req, pending):
            return RiskDecision(True, is_exit=True)
        reason = self._entry_violation(req, pending)
        if reason:
            self.rejections += 1
            log.warning(
                "order rejected by risk",
                extra={
                    "strategy": req.strategy_id,
                    "symbol": req.instrument.tradingsymbol,
                    "reason": reason,
                },
            )
            return RiskDecision(False, is_exit=False, reason=reason)
        self._recent.append((self._clock.now(), req))
        return RiskDecision(True, is_exit=False)

    def _entry_violation(self, req: OrderRequest, pending: list[Order]) -> str:
        lim = self.limits
        inst = req.instrument
        now = self._clock.now()
        if self.kill_switch.tripped:
            return f"kill switch active: {self.kill_switch.reason}"
        if req.quantity % inst.lot_size:
            return f"quantity {req.quantity} is not a multiple of lot size {inst.lot_size}"
        lots = req.quantity // inst.lot_size
        if lots > lim.max_lots_per_order:
            return f"{lots} lots exceeds max {lim.max_lots_per_order} per order"
        if not (lim.trading_start <= now.time() < lim.trading_end):
            return f"outside trading window {lim.trading_start:%H:%M}-{lim.trading_end:%H:%M}"
        pos = self._positions.get(req.strategy_id, inst.token, req.product)
        opens_new = pos is None or pos.quantity == 0
        if opens_new:
            open_keys = {p.key for p in self._positions.open_positions()}
            pending_new = {
                (o.request.strategy_id, o.request.instrument.token, o.request.product)
                for o in pending
            } - open_keys
            if len(open_keys) + len(pending_new) >= lim.max_open_positions:
                return f"max open positions ({lim.max_open_positions}) reached"
        cutoff = now - timedelta(minutes=1)
        while self._recent and self._recent[0][0] <= cutoff:
            self._recent.popleft()
        if len(self._recent) >= lim.max_orders_per_minute:
            return f"max {lim.max_orders_per_minute} orders per minute reached"
        dup_cutoff = now - timedelta(seconds=lim.duplicate_window_sec)
        for ts, prev in reversed(self._recent):
            if ts <= dup_cutoff:
                break
            if (
                prev.strategy_id == req.strategy_id
                and prev.instrument.token == inst.token
                and prev.side == req.side
                and prev.quantity == req.quantity
            ):
                return "duplicate order within guard window"
        return ""

    # --- P&L -------------------------------------------------------------------

    def check_daily_loss(self, net_pnl: float) -> bool:
        """Trip the kill switch when the day's net P&L breaches the limit.
        Returns True only on the call that trips it."""
        if net_pnl <= -self.limits.daily_loss_limit:
            return self.kill_switch.trip(
                f"daily loss limit breached: net P&L {net_pnl:.2f} <= "
                f"-{self.limits.daily_loss_limit:.2f}",
                self._clock.now(),
            )
        return False

    def reset_day(self) -> None:
        """Start-of-day reset (09:00 job). The kill switch is reset separately, by a human."""
        self._recent.clear()
        self.rejections = 0
