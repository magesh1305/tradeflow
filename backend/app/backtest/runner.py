"""Backtest runner: replays stored candles day by day through the real TradingEngine
with PaperExecutor (wired by `factory.build_backtest_engine`, which always uses paper).

Each weekday in the range:
  1. load the instruments that were live that day (from instrument history);
  2. reset daily risk counters (an earlier day's kill switch is reset too, and logged:
     in production a human does that the next morning);
  3. start the strategy fresh (live restarts the process daily, so strategies start
     clean each morning); NRML positions carry over;
  4. replay the day; the engine's 15:15 square-off flattens MIS;
  5. stop the strategy and record end-of-day equity.
A weekday with no candles for any subscribed token is reported as a non-trading
day (holiday). Gaps on trading days are reported as missing data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app import factory
from app.backtest.metrics import Metrics, compute_metrics
from app.config import Settings
from app.core.clock import IST, ManualClock
from app.data.historical import HistoryStore, session_bounds
from app.data.instruments import InstrumentStore
from app.feeds.historical import HistoricalFeed
from app.notify.base import LogNotifier
from app.strategies.registry import StrategyConfig

log = logging.getLogger(__name__)


@dataclass
class BacktestRequest:
    strategy: str
    params: dict[str, Any]
    start: date
    end: date
    interval_seconds: int = 60
    strategy_id: str = "backtest"


@dataclass
class BacktestResult:
    metrics: Metrics
    equity_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    missing_data: list[dict[str, Any]]
    non_trading_days: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.as_dict(),
            "missing_data": len(self.missing_data),
            "non_trading_days": self.non_trading_days,
            "notes": self.notes,
        }


def weekdays(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


async def run_backtest(  # noqa: PLR0915 - one linear day loop
    settings: Settings, req: BacktestRequest, store: HistoryStore
) -> BacktestResult:
    if req.end < req.start:
        raise ValueError("end date before start date")
    capital = settings.paper_capital
    instruments = InstrumentStore()
    history = await store.load_instruments(req.start, req.end)
    by_token: dict[int, Any] = {}
    feed = HistoricalFeed(store, interval=req.interval_seconds, instrument=by_token.get)
    clock = ManualClock(datetime.combine(req.start, datetime.min.time(), IST))
    notifier = LogNotifier()
    cfg = StrategyConfig(id=req.strategy_id, strategy=req.strategy, enabled=True, params=req.params)
    engine = factory.build_backtest_engine(
        settings,
        instruments=instruments,
        feed=feed,
        clock=clock,
        notifier=notifier,
        strategy_configs=[cfg],
    )
    sid = req.strategy_id
    await engine.start([])

    equity_curve: list[dict[str, Any]] = []
    daily_net: list[float] = []
    equity: list[float] = []
    non_trading: list[str] = []
    notes: list[str] = []
    prev_net = 0.0
    try:
        for day in weekdays(req.start, req.end):
            live = [i for i, first in history if first <= day]
            instruments.load(live, day)
            by_token.clear()
            by_token.update({i.token: i for i in instruments.all()})
            if len(instruments) == 0:
                non_trading.append(day.isoformat())
                notes.append(f"{day}: no instrument history; day skipped")
                continue
            open_, close = session_bounds(day)
            engine.advance_to(open_ - timedelta(minutes=1))
            engine.risk.reset_day()
            if engine.risk.kill_switch.tripped:
                notes.append(f"{day}: kill switch from previous day reset by backtester")
                engine.risk.kill_switch.reset()
            missing_before = len(feed.missing)
            emitted_before = feed.ticks_emitted
            try:
                engine.start_strategy(sid)
            except Exception as exc:
                notes.append(f"{day}: strategy failed to start: {exc}")
                continue
            await feed.play_day(day, engine.drain)
            engine.advance_to(close)
            await engine.drain()
            engine.stop_strategy(sid)
            slot = engine.slots[sid]
            if slot.error:
                notes.append(f"{day}: strategy error: {slot.error}")
            if feed.ticks_emitted == emitted_before:
                # Nothing at all for this day: a market holiday, not a data gap.
                del feed.missing[missing_before:]
                non_trading.append(day.isoformat())
                continue
            pnl = engine.positions.pnl()
            daily_net.append(pnl.net - prev_net)
            prev_net = pnl.net
            equity.append(pnl.net)
            equity_curve.append(
                {
                    "date": day.isoformat(),
                    "net": round(pnl.net, 2),
                    "gross": round(pnl.gross, 2),
                    "charges": round(pnl.charges, 2),
                    "open_positions": len(engine.positions.open_positions()),
                }
            )
    finally:
        await engine.stop()

    total = engine.positions.pnl()
    metrics, trades = compute_metrics(
        engine.fills,
        engine.fill_charges,
        daily_net=daily_net,
        equity=equity,
        capital=capital,
        gross_total=total.gross,
        charges_total=total.charges,
    )
    if engine.positions.open_positions():
        notes.append("positions still open at the end of the range (marked to last price)")
    return BacktestResult(
        metrics=metrics,
        equity_curve=equity_curve,
        trades=[t.as_dict() for t in trades],
        missing_data=[m.as_dict() for m in feed.missing],
        non_trading_days=non_trading,
        notes=notes,
    )
