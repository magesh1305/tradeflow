"""SqlHistoryStore and SqlBacktestStore against TimescaleDB (CI). Skipped when unreachable."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.backtest.runner import BacktestRequest
from app.backtest.store import SqlBacktestStore
from app.core.clock import IST
from app.core.models import Candle
from app.data.historical import SqlHistoryStore
from app.storage.db import make_engine, make_sessionmaker
from tests.builders import CE, FUT
from tests.test_migrations import URL, _reachable

pytestmark = pytest.mark.integration
BACKEND = Path(__file__).resolve().parents[1]


async def test_sql_history_and_backtest_stores() -> None:
    if not URL or not await _reachable(URL):
        pytest.skip("TEST_DATABASE_URL not set or unreachable")
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": URL},
        check=True,
    )
    engine = make_engine(URL)
    try:
        async with engine.begin() as conn:
            for t in ("candles", "instruments", "backtest_runs"):
                await conn.execute(text(f"DELETE FROM {t}"))  # noqa: S608 - fixed names
        sessions = make_sessionmaker(engine)
        hist = SqlHistoryStore(sessions)
        t0 = datetime(2025, 11, 3, 9, 15, tzinfo=IST)
        candles = [
            Candle(FUT.token, 60, t0 + timedelta(minutes=i), 1, 2, 0.5, 1.5, 10, 5)
            for i in range(1500)
        ]  # > one 1000-row batch
        assert await hist.upsert_candles(candles) == 1500
        await hist.upsert_candles(candles[:10])  # upsert, no duplicates
        got = await hist.load_candles(FUT.token, 60, t0, t0 + timedelta(days=2))
        assert len(got) == 1500 and got[0].ts == t0

        await hist.upsert_instruments([FUT, CE], date(2025, 11, 3))
        await hist.upsert_instruments([FUT], date(2025, 11, 1))  # earlier sighting wins
        insts = dict(
            (i.key, first)
            for i, first in await hist.load_instruments(date(2025, 11, 3), date(2025, 11, 3))
        )
        assert insts[FUT.key] == date(2025, 11, 1)
        assert CE.key in insts

        runs = SqlBacktestStore(sessions)
        rid = await runs.create(
            BacktestRequest("ema_crossover", {"fast": 3}, date(2025, 11, 3), date(2025, 11, 4))
        )
        await runs.fail(rid, "boom")
        row = await runs.get(rid)
        assert row is not None and row["status"] == "failed" and row["error"] == "boom"
        assert (await runs.list())[0]["id"] == rid
    finally:
        await engine.dispose()


async def test_sql_plan_store() -> None:
    if not URL or not await _reachable(URL):
        pytest.skip("TEST_DATABASE_URL not set or unreachable")
    from app.storage.plans import SqlPlanStore  # noqa: PLC0415

    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": URL},
        check=True,
    )
    engine = make_engine(URL)
    try:
        async with engine.begin() as conn:
            for t in ("plan_events", "daily_plans"):
                await conn.execute(text(f"DELETE FROM {t}"))  # noqa: S608 - fixed names
        store = SqlPlanStore(make_sessionmaker(engine))
        t0 = datetime(2025, 11, 3, 9, 0, tzinfo=IST)
        d1, d2 = date(2025, 11, 3), date(2025, 11, 4)
        await store.save(d1, "paper", {"v": 1}, now=t0, changes=["plan submitted"], loosened=[])
        await store.save(
            d1, "paper", {"v": 2}, now=t0 + timedelta(hours=2), changes=["x"], loosened=["y"]
        )
        await store.save(d2, "paper", {"v": 3}, now=t0 + timedelta(days=1), changes=[], loosened=[])
        got = await store.get(d1, "paper")
        assert got is not None and got.plan == {"v": 2}
        assert got.submitted_at == t0  # first submission time is kept
        assert await store.get(d1, "live") is None
        latest = await store.latest("paper")
        assert latest is not None and latest.trading_date == d2
        events = await store.events(d1, "paper")
        assert [e["loosened"] for e in events] == [["y"], []]
    finally:
        await engine.dispose()
