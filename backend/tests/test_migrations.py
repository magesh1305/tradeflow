"""Runs Alembic against a real TimescaleDB. Skipped when TEST_DATABASE_URL is unset
or unreachable (CI provides it as a service)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.storage.models import Base

BACKEND = Path(__file__).resolve().parents[1]
URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.integration


async def _reachable(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with asyncio.timeout(5), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


def _alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": URL}
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, check=True
    )


async def test_upgrade_creates_schema_and_hypertable() -> None:
    if not URL or not await _reachable(URL):
        pytest.skip("TEST_DATABASE_URL not set or unreachable")

    await asyncio.to_thread(_alembic, "downgrade", "base")
    await asyncio.to_thread(_alembic, "upgrade", "head")

    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            tables = {r[0] for r in rows}
            assert set(Base.metadata.tables) <= tables

            hyper = await conn.execute(
                text("SELECT hypertable_name FROM timescaledb_information.hypertables")
            )
            assert "candles" in {r[0] for r in hyper}

            # Unique (token, interval, ts): upserting the same candle keeps one row.
            insert = text(
                "INSERT INTO candles VALUES (1, 'minute', '2025-11-03 09:15+05:30', "
                "1, 2, 0.5, 1.5, 10, 0) ON CONFLICT (instrument_token, interval, ts) "
                "DO UPDATE SET close = EXCLUDED.close"
            )
            await conn.execute(insert)
            await conn.execute(insert)
            count = await conn.scalar(text("SELECT count(*) FROM candles"))
            assert count == 1
            await conn.rollback()
    finally:
        await engine.dispose()

    await asyncio.to_thread(_alembic, "downgrade", "base")
    await asyncio.to_thread(_alembic, "upgrade", "head")
