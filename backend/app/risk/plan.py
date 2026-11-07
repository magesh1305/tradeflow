"""The daily trading plan: the risk limits an operator reviews and submits every
morning from the dashboard's Settings tab.

- No strategy can start on a trading day until that day's plan is submitted.
- Account limits: daily loss limit (kill switch), optional profit target (stops
  new trades only), optional max trades per day, max open positions, max lots per
  order, trading window.
- Per strategy: enabled, lots, max trades per day, loss limit (stop and exit),
  profit target (stops new trades only), max lots per order, max capital (margin
  estimate).
- "Trades" are new entries. Exits, stops and square-offs never count, and are
  never blocked. A multi-leg group counts as one trade.
- During market hours a submitted plan can always be tightened. Loosening any
  limit needs an explicit confirmation, and is logged and alerted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

LOOSEN_CONFIRMATION = "LOOSEN"


class AccountPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_loss_limit: float = Field(gt=0, description="INR, net of charges; trips the kill switch")
    profit_target: float | None = Field(
        default=None, gt=0, description="INR, net; reached = no new trades today"
    )
    max_trades_per_day: int | None = Field(
        default=None, ge=1, description="new entries, all strategies"
    )
    max_open_positions: int = Field(ge=1)
    max_lots_per_order: int = Field(ge=1)
    trading_start: time
    trading_end: time

    @model_validator(mode="after")
    def _window(self) -> AccountPlan:
        if self.trading_start >= self.trading_end:
            raise ValueError("trading_start must be before trading_end")
        return self


class StrategyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    lots: int | None = Field(default=None, ge=1, description="sets the strategy's `lots` param")
    max_trades_per_day: int | None = Field(default=None, ge=1)
    loss_limit: float | None = Field(default=None, gt=0, description="INR net; hit = stop and exit")
    profit_target: float | None = Field(
        default=None, gt=0, description="INR net; hit = no new trades"
    )
    max_lots_per_order: int | None = Field(default=None, ge=1)
    max_capital: float | None = Field(default=None, gt=0, description="INR margin estimate")


class DailyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: AccountPlan
    strategies: dict[str, StrategyPlan] = Field(default_factory=dict)


# --- what counts as loosening ------------------------------------------------------


def _looser_cap(old: float | None, new: float | None) -> bool:
    """For an upper limit where None means 'no limit': is `new` more permissive?"""
    if old is None:
        return False
    return new is None or new > old


def loosened(old: DailyPlan, new: DailyPlan) -> list[str]:
    """Human-readable list of every limit that `new` makes more permissive than `old`."""
    out: list[str] = []
    a, b = old.account, new.account
    caps: list[tuple[str, Callable[[AccountPlan], float | None]]] = [
        ("daily loss limit", lambda p: p.daily_loss_limit),
        ("profit target", lambda p: p.profit_target),
        ("max trades per day", lambda p: p.max_trades_per_day),
        ("max open positions", lambda p: p.max_open_positions),
        ("max lots per order", lambda p: p.max_lots_per_order),
    ]
    for label, get in caps:
        if _looser_cap(get(a), get(b)):
            out.append(
                f"account {label}: {get(a)} -> {get(b) if get(b) is not None else 'no limit'}"
            )
    if b.trading_start < a.trading_start:
        out.append(f"account trading start: {a.trading_start:%H:%M} -> {b.trading_start:%H:%M}")
    if b.trading_end > a.trading_end:
        out.append(f"account trading end: {a.trading_end:%H:%M} -> {b.trading_end:%H:%M}")

    fields: list[str] = [
        "lots",
        "max_trades_per_day",
        "loss_limit",
        "profit_target",
        "max_lots_per_order",
        "max_capital",
    ]
    for sid, new_s in new.strategies.items():
        old_s = old.strategies.get(sid)
        if old_s is None:
            continue
        for f in fields:
            ov: Any = getattr(old_s, f)
            nv: Any = getattr(new_s, f)
            if f == "lots":
                if ov is not None and nv is not None and nv > ov:
                    out.append(f"{sid} lots: {ov} -> {nv}")
            elif _looser_cap(ov, nv):
                out.append(
                    f"{sid} {f.replace('_', ' ')}: {ov} -> {nv if nv is not None else 'no limit'}"
                )
    return out


def describe_changes(old: DailyPlan | None, new: DailyPlan) -> list[str]:
    """Every field that differs, for the audit trail and the alert."""
    if old is None:
        return ["plan submitted"]
    out: list[str] = []
    for k, v in new.account.model_dump().items():
        ov = getattr(old.account, k)
        if ov != v:
            out.append(f"account {k}: {ov} -> {v}")
    for sid, s in new.strategies.items():
        o = old.strategies.get(sid)
        for k, v in s.model_dump().items():
            ov = getattr(o, k) if o is not None else None
            if ov != v:
                out.append(f"{sid} {k}: {ov} -> {v}")
    return out
