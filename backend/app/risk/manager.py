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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app.config import Settings
from app.core.clock import Clock
from app.core.models import Instrument, Order, OrderRequest, Side, Tick
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
    expiry_no_entry_after: time = time(14, 30)
    option_max_spread_pct: float = 5.0
    option_min_volume: int = 0
    freeze_limits: dict[str, int] = field(default_factory=dict)
    max_slices: int = 10  # Kite auto-slice limit

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
            expiry_no_entry_after=s.expiry_no_entry_after,
            option_max_spread_pct=s.option_max_spread_pct,
            option_min_volume=s.option_min_volume,
            freeze_limits=dict(s.freeze_limits),
        )


@dataclass(frozen=True, slots=True)
class StrategyLimits:
    """Per-strategy limits from the daily plan (None = no limit)."""

    max_trades_per_day: int | None = None
    loss_limit: float | None = None
    profit_target: float | None = None
    max_lots_per_order: int | None = None
    max_capital: float | None = None


@dataclass(frozen=True, slots=True)
class PlanLimits:
    """Daily-plan limits on top of RiskLimits (None = no limit)."""

    profit_target: float | None = None
    max_trades_per_day: int | None = None
    strategies: dict[str, StrategyLimits] = field(default_factory=dict)


# (instrument, side, quantity, price) -> estimated margin in INR
MarginEstimator = Callable[[Instrument, Side, int, float | None], float]


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
        self.plan = PlanLimits()
        self.estimator: MarginEstimator | None = None
        # Today's new entries per strategy, keyed by leg group (one trade) or order id.
        self._entries: dict[str, set[str]] = {}

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
            and not o.request.protective  # a resting stop must not block a real exit
        )
        return req.quantity <= abs(pos.quantity) - already_exiting

    # --- checks ----------------------------------------------------------------

    def check(
        self, req: OrderRequest, pending: Iterable[Order] = (), quote: Tick | None = None
    ) -> RiskDecision:
        pending = list(pending)
        if self.is_exit(req, pending):
            return RiskDecision(True, is_exit=True)
        reason = (
            self._entry_violation(req, pending)
            or self._plan_violation(req, pending)
            or self._liquidity_violation(req, quote)
        )
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
        if not req.protective:
            self._entries.setdefault(req.strategy_id, set()).add(
                req.leg_group_id or req.client_order_id
            )
        return RiskDecision(True, is_exit=False)

    # --- daily plan limits --------------------------------------------------------

    def trades_today(self, strategy_id: str | None = None) -> int:
        if strategy_id is not None:
            return len(self._entries.get(strategy_id, ()))
        return sum(len(v) for v in self._entries.values())

    def _new_trade(self, req: OrderRequest) -> bool:
        """A leg of a group already counted is not a new trade."""
        return not (req.leg_group_id and req.leg_group_id in self._entries.get(req.strategy_id, ()))

    def _plan_violation(self, req: OrderRequest, pending: list[Order]) -> str:
        plan = self.plan
        sid = req.strategy_id
        lim = plan.strategies.get(sid, StrategyLimits())
        if plan.profit_target is not None and self._positions.pnl().net >= plan.profit_target:
            return f"account profit target {plan.profit_target:,.0f} reached: no new trades today"
        if lim.profit_target is not None and self._positions.pnl(sid).net >= lim.profit_target:
            return f"{sid} profit target {lim.profit_target:,.0f} reached: no new trades today"
        new = self._new_trade(req)
        if (
            new
            and plan.max_trades_per_day is not None
            and (self.trades_today() >= plan.max_trades_per_day)
        ):
            return f"max {plan.max_trades_per_day} trades per day reached (account)"
        if (
            new
            and lim.max_trades_per_day is not None
            and (self.trades_today(sid) >= lim.max_trades_per_day)
        ):
            return f"max {lim.max_trades_per_day} trades per day reached ({sid})"
        lots = req.quantity // req.instrument.lot_size
        if lim.max_lots_per_order is not None and lots > lim.max_lots_per_order:
            return f"{lots} lots exceeds {sid} max {lim.max_lots_per_order} per order"
        if lim.max_capital is not None and self.estimator is not None:
            est = self.estimator
            used = sum(
                est(
                    p.instrument,
                    Side.BUY if p.quantity > 0 else Side.SELL,
                    abs(p.quantity),
                    p.average_price,
                )
                for p in self._positions.open_positions(sid)
            ) + sum(
                est(o.request.instrument, o.request.side, o.pending_quantity, o.request.price)
                for o in pending
                if o.request.strategy_id == sid and not o.request.protective
            )
            need = est(req.instrument, req.side, req.quantity, req.price)
            if used + need > lim.max_capital:
                return (
                    f"{sid} capital limit {lim.max_capital:,.0f}: would use "
                    f"{used + need:,.0f} (estimate)"
                )
        return ""

    def strategies_over_loss_limit(self) -> list[tuple[str, float, float]]:
        """(strategy, net P&L, limit) for strategies whose net loss reached their limit."""
        out = []
        for sid, lim in self.plan.strategies.items():
            if lim.loss_limit is not None:
                net = self._positions.pnl(sid).net
                if net <= -lim.loss_limit:
                    out.append((sid, net, lim.loss_limit))
        return out

    def profit_targets_reached(self) -> list[str]:
        """'account' and/or strategy ids whose profit target is reached."""
        out = []
        if self.plan.profit_target is not None and (
            self._positions.pnl().net >= self.plan.profit_target
        ):
            out.append("account")
        for sid, lim in self.plan.strategies.items():
            if lim.profit_target is not None and self._positions.pnl(sid).net >= lim.profit_target:
                out.append(sid)
        return out

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
        freeze = lim.freeze_limits.get(inst.name)
        if freeze and req.quantity > freeze * lim.max_slices:
            return f"quantity {req.quantity} exceeds {lim.max_slices} x freeze limit {freeze}"
        if inst.expiry == now.date() and now.time() >= lim.expiry_no_entry_after:
            return f"no new entries on expiry day after {lim.expiry_no_entry_after:%H:%M}"
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

    def _liquidity_violation(self, req: OrderRequest, quote: Tick | None) -> str:
        """Option entries only: reject strikes with no quote, a wide spread or thin volume."""
        if not req.instrument.is_option:
            return ""
        lim = self.limits
        if quote is None:
            return "no quote for option (subscribe it before trading)"
        if not quote.bid or not quote.ask:
            return "option has no two-sided quote"
        mid = (quote.bid + quote.ask) / 2
        spread_pct = (quote.ask - quote.bid) / mid * 100
        if spread_pct > lim.option_max_spread_pct:
            return f"spread {spread_pct:.1f}% exceeds {lim.option_max_spread_pct:.1f}%"
        if quote.volume < lim.option_min_volume:
            return f"volume {quote.volume} below minimum {lim.option_min_volume}"
        return ""

    def orders_last_minute(self) -> int:
        cutoff = self._clock.now() - timedelta(minutes=1)
        return sum(1 for ts, _ in self._recent if ts > cutoff)

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
        self._entries.clear()
