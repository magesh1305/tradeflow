"""Backtest metrics: round-trip trades, win rate, drawdown, daily Sharpe."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.core.models import Fill, Product


@dataclass(frozen=True, slots=True)
class Trade:
    """One round trip: from flat to flat (a flip closes one trade and opens the next)."""

    strategy_id: str
    symbol: str
    product: str
    opened: datetime
    closed: datetime
    gross: float
    charges: float

    @property
    def net(self) -> float:
        return self.gross - self.charges

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["opened"], d["closed"], d["net"] = (
            self.opened.isoformat(),
            self.closed.isoformat(),
            self.net,
        )
        return d


@dataclass
class _Open:
    qty: int = 0
    avg: float = 0.0
    gross: float = 0.0
    charges: float = 0.0
    opened: datetime | None = None


def build_trades(fills: list[Fill], charges: dict[str, float]) -> list[Trade]:
    trades: list[Trade] = []
    book: dict[tuple[str, int, Product], _Open] = {}
    for f in sorted(fills, key=lambda f: f.ts):
        key = (f.strategy_id, f.instrument.token, f.product)
        st = book.setdefault(key, _Open())
        cost = charges.get(f.fill_id, 0.0)
        q = f.signed_quantity
        if st.qty == 0:
            st.opened, st.avg, st.qty, st.gross, st.charges = f.ts, f.price, q, 0.0, cost
            continue
        if (st.qty > 0) == (q > 0):  # add
            new = st.qty + q
            st.avg = (st.avg * abs(st.qty) + f.price * abs(q)) / abs(new)
            st.qty = new
            st.charges += cost
            continue
        closing = min(abs(q), abs(st.qty))
        direction = 1 if st.qty > 0 else -1
        st.gross += (f.price - st.avg) * closing * direction
        st.charges += cost
        st.qty += closing * -direction
        remaining = q + closing * direction
        if st.qty == 0:
            assert st.opened is not None  # noqa: S101 - set when the trade opened
            trades.append(
                Trade(
                    f.strategy_id,
                    f.instrument.tradingsymbol,
                    str(f.product),
                    st.opened,
                    f.ts,
                    st.gross,
                    st.charges,
                )
            )
            st.gross = st.charges = 0.0
            if remaining:
                st.opened, st.avg, st.qty = f.ts, f.price, remaining
    return trades


def max_drawdown(equity: list[float], capital: float) -> tuple[float, float]:
    """Largest peak-to-trough fall in INR, and as % of (capital + peak equity)."""
    peak = 0.0
    worst = 0.0
    worst_pct = 0.0
    for e in equity:
        peak = max(peak, e)
        dd = peak - e
        if dd > worst:
            worst = dd
            worst_pct = dd / (capital + peak) * 100 if capital + peak > 0 else 0.0
    return worst, worst_pct


def daily_sharpe(daily_pnl: list[float], capital: float) -> float | None:
    """Annualised (sqrt 252) Sharpe of daily returns on capital; risk-free rate 0."""
    if len(daily_pnl) < 2 or capital <= 0:
        return None
    rets = [p / capital for p in daily_pnl]
    sd = statistics.stdev(rets)
    if sd == 0:
        return None
    return statistics.mean(rets) / sd * math.sqrt(252)


@dataclass
class Metrics:
    gross_pnl: float
    charges: float
    net_pnl: float
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    max_drawdown: float
    max_drawdown_pct: float
    sharpe: float | None
    days: int
    fills: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_metrics(
    fills: list[Fill],
    charges: dict[str, float],
    *,
    daily_net: list[float],
    equity: list[float],
    capital: float,
    gross_total: float,
    charges_total: float,
) -> tuple[Metrics, list[Trade]]:
    trades = build_trades(fills, charges)
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net <= 0]
    dd, dd_pct = max_drawdown(equity, capital)
    m = Metrics(
        gross_pnl=round(gross_total, 2),
        charges=round(charges_total, 2),
        net_pnl=round(gross_total - charges_total, 2),
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / len(trades) * 100, 2) if trades else None,
        avg_win=round(statistics.mean(wins), 2) if wins else None,
        avg_loss=round(statistics.mean(losses), 2) if losses else None,
        max_drawdown=round(dd, 2),
        max_drawdown_pct=round(dd_pct, 3),
        sharpe=round(s, 3) if (s := daily_sharpe(daily_net, capital)) is not None else None,
        days=len(daily_net),
        fills=len(fills),
    )
    return m, trades
