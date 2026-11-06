"""Backtests over synthetic stored candles, through the real engine and runner."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from kiteconnect import KiteConnect

from app.backtest.metrics import build_trades, daily_sharpe, max_drawdown
from app.backtest.runner import BacktestRequest, run_backtest, weekdays
from app.backtest.store import MemoryBacktestStore, execute
from app.brokers.kite import KiteBroker
from app.cli import cmd_backtest, parser
from app.config import Settings
from app.core.clock import IST, ManualClock
from app.core.models import Candle, Side
from app.data.historical import MemoryHistoryStore, session_bounds
from app.data.instruments import InstrumentStore
from app.feeds.random_walk import RandomWalkFeed, synthetic_nifty
from app.notify.base import LogNotifier
from app.runtime import Runtime
from app.storage.journal import Journal, MemoryJournalStore
from tests.builders import FUT, fill
from tests.test_live_executor import FastLimiter

DAYS = [date(2025, 11, 3), date(2025, 11, 4), date(2025, 11, 5)]


_CACHE: dict[tuple[Any, ...], MemoryHistoryStore] = {}


async def synth_history(days: list[date], seed: int = 1, skip: Any = None) -> MemoryHistoryStore:
    """1-minute candles for the synthetic NIFTY universe: 4 random-walk steps per minute.
    Cached per arguments (backtests only read the store; generating is the slow part)."""
    key = (tuple(days), seed)
    if skip is None and key in _CACHE:
        return _CACHE[key]
    store = MemoryHistoryStore()
    if skip is None:
        _CACHE[key] = store
    for n, d in enumerate(days):
        u = synthetic_nifty(d)
        await store.upsert_instruments(u.all, d)
        open_, _ = session_bounds(d)
        walk = RandomWalkFeed(
            u,
            realtime=False,
            start=open_,
            end=open_,
            seed=seed + n,
            annual_vol=0.25,
            step=timedelta(seconds=15),
        )
        candles: list[Candle] = []
        for m in range(375):
            ts = open_ + timedelta(minutes=m)
            path: dict[int, list[float]] = {i.token: [] for i in u.all}
            for _ in range(4):
                walk._advance()
                for inst in u.all:
                    path[inst.token].append(walk.price_of(inst, ts))
            for inst in u.all:
                p = path[inst.token]
                if skip and skip(d, inst, m):
                    continue
                candles.append(
                    Candle(inst.token, 60, ts, p[0], max(p), min(p), p[-1], volume=50_000)
                )
        await store.upsert_candles(candles)
    return store


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)


async def test_ema_backtest_end_to_end() -> None:
    store = await synth_history(DAYS)
    req = BacktestRequest(
        "ema_crossover", {"interval_minutes": 1, "fast": 3, "slow": 8}, DAYS[0], DAYS[-1]
    )
    res = await run_backtest(settings(), req, store)
    m = res.metrics
    assert m.days == 3 and len(res.equity_curve) == 3
    assert m.trades > 0 and m.fills > 0
    assert m.charges > 0
    assert m.net_pnl == pytest.approx(m.gross_pnl - m.charges, abs=0.02)
    assert res.equity_curve[-1]["net"] == pytest.approx(m.net_pnl, abs=0.02)
    assert res.equity_curve[-1]["open_positions"] == 0  # MIS squared off daily
    assert res.missing_data == []
    assert m.win_rate is not None and 0 <= m.win_rate <= 100
    assert m.max_drawdown >= 0
    assert sum(t["net"] for t in res.trades) == pytest.approx(m.net_pnl, abs=1.0)


async def test_straddle_backtest_restarts_daily_and_loads_options_midday() -> None:
    store = await synth_history(DAYS[:2], seed=7)
    req = BacktestRequest("short_straddle", {"stop_loss_pct": 200}, DAYS[0], DAYS[1])
    res = await run_backtest(settings(daily_loss_limit=10_000_000), req, store)
    assert res.metrics.days == 2
    # Two legs each day (strategy restarted every morning), each exited the same day.
    assert res.metrics.trades == 4
    assert res.missing_data == []


async def test_missing_data_and_holidays_reported_not_skipped() -> None:
    gap_day = DAYS[1]
    store = await synth_history(
        DAYS[:2],
        skip=lambda d, inst, m: (
            d == gap_day and inst.token == FUT.token * 0 + 9_000_001 and m > 100
        ),
    )
    req = BacktestRequest(
        "ema_crossover", {"interval_minutes": 1, "fast": 3, "slow": 8}, DAYS[0], date(2025, 11, 7)
    )  # Thu/Fri have no data at all
    res = await run_backtest(settings(), req, store)
    (gap,) = res.missing_data
    assert gap["date"] == gap_day.isoformat()
    assert gap["found_candles"] == 101 and gap["expected_candles"] == 375
    assert res.non_trading_days == ["2025-11-05", "2025-11-06", "2025-11-07"]
    assert res.metrics.days == 2


async def test_backtest_is_always_paper_even_in_live_mode() -> None:
    from app import factory  # noqa: PLC0415
    from app.execution.paper import PaperExecutor  # noqa: PLC0415

    live = settings(
        mode="live",
        live_trading_confirmed=True,
        admin_token="x" * 16,
        feed="kite",
        kite_api_key="k",
        kite_api_secret="s",
    )
    engine = factory.build_backtest_engine(
        live,
        instruments=InstrumentStore(),
        feed=MagicMock(),
        clock=ManualClock(datetime(2025, 11, 3, tzinfo=IST)),
        notifier=LogNotifier(),
        strategy_configs=[],
    )
    assert isinstance(engine.executor, PaperExecutor)


async def test_runs_are_stored_with_metrics_or_failure() -> None:
    store = await synth_history(DAYS[:1])
    runs = MemoryBacktestStore()
    req = BacktestRequest(
        "ema_crossover", {"interval_minutes": 1, "fast": 3, "slow": 8}, DAYS[0], DAYS[0]
    )
    run_id, _ = await execute(settings(), req, store, runs)
    row = await runs.get(run_id)
    assert row is not None and row["status"] == "done"
    assert row["metrics"]["days"] == 1 and row["equity_curve"]
    bad = BacktestRequest("nope", {}, DAYS[0], DAYS[0])
    with pytest.raises(Exception, match="unknown strategy"):
        await execute(settings(), bad, store, runs)
    failed = (await runs.list())[0]
    assert failed["status"] == "failed" and "unknown strategy" in failed["error"]


async def test_cli_backtest(capsys: pytest.CaptureFixture[str]) -> None:
    store = await synth_history(DAYS[:1])
    args = parser().parse_args(
        [
            "backtest",
            "--strategy",
            "ema_crossover",
            "--params",
            '{"interval_minutes": 1, "fast": 3, "slow": 8}',
            "--from",
            "2025-11-03",
            "--to",
            "2025-11-03",
        ]
    )
    out = await cmd_backtest(args, settings(), store, MemoryBacktestStore())
    printed = json.loads(capsys.readouterr().out)
    assert printed["run_id"] == out["run_id"] == 1
    assert printed["metrics"]["days"] == 1


def test_cli_parser_download() -> None:
    a = parser().parse_args(
        [
            "download-history",
            "--underlying",
            "NIFTY",
            "--underlying",
            "BANKNIFTY",
            "--from",
            "2025-10-01",
            "--to",
            "2025-10-31",
        ]
    )
    assert a.underlying == ["NIFTY", "BANKNIFTY"] and a.interval == "minute"


# --- metrics -------------------------------------------------------------------------------


def test_build_trades_round_trips_flips_and_charges() -> None:
    f1 = fill(Side.BUY, 75, 100.0)
    f2 = fill(Side.SELL, 150, 110.0)  # closes long (+750), opens short 75
    f3 = fill(Side.BUY, 75, 105.0)  # closes short (+375)
    for i, f in enumerate((f1, f2, f3)):
        object.__setattr__(f, "ts", f.ts + timedelta(minutes=i))
    trades = build_trades([f1, f2, f3], {f1.fill_id: 10.0, f2.fill_id: 20.0, f3.fill_id: 5.0})
    assert [(t.gross, t.charges) for t in trades] == [(750.0, 30.0), (375.0, 5.0)]
    assert trades[0].net == 720.0


def test_drawdown_and_sharpe() -> None:
    dd, pct = max_drawdown([100, 300, 50, 400, 350], capital=1000)
    assert dd == 250 and pct == pytest.approx(250 / 1300 * 100)
    assert daily_sharpe([100, 100, 100], 1000) is None  # zero variance
    assert daily_sharpe([100], 1000) is None
    s = daily_sharpe([100, -50, 80, 20], 100_000)
    assert s is not None and s > 0


def test_weekdays() -> None:
    assert weekdays(date(2025, 11, 7), date(2025, 11, 10)) == [
        date(2025, 11, 7),
        date(2025, 11, 10),
    ]


# --- daily recording job -------------------------------------------------------------------


async def test_runtime_records_candles_and_reports(tmp_path: Any) -> None:
    d = DAYS[0]
    u = synthetic_nifty(d)
    master = InstrumentStore()
    master.load(u.all, d)
    kite = MagicMock(spec=KiteConnect)
    open_, _ = session_bounds(d)
    kite.historical_data.side_effect = lambda token, a, b, interval, **k: [
        {
            "date": (open_ + timedelta(minutes=i)).replace(tzinfo=None),
            "open": 24000.0,
            "high": 24001.0,
            "low": 23999.0,
            "close": 24000.0,
            "volume": 1,
            "oi": 0,
        }
        for i in range(3)
    ]
    notifier = LogNotifier()
    s = settings(record_underlyings=["NIFTY"], record_strikes_width=1, record_expiries=1)
    history = MemoryHistoryStore()
    rt = Runtime(
        s,
        notifier=notifier,
        journal=Journal(MemoryJournalStore(), "paper", notifier),
        auth=None,
        clock=ManualClock(datetime(2025, 11, 3, 15, 40, tzinfo=IST)),
        history=history,
    )
    rt.engine = MagicMock()
    rt.engine.instruments = master
    rt.broker = KiteBroker(kite, orders_enabled=False, historical_limiter=FastLimiter())
    await rt.record_candles()
    assert (notifier.sent[-1][0],) == ("info",)
    assert "Recorded" in notifier.sent[-1][1]
    tokens = {tok for tok, _ in history.candles}
    assert u.index.token in tokens and u.future.token in tokens
    assert len(tokens) == 2 + 3 * 2  # spot, future, 3 strikes x CE/PE
    assert len(history.instruments) == len(tokens)
