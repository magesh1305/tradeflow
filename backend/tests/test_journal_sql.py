"""SqlJournalStore against real TimescaleDB (CI service). Skipped when unreachable."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.models import Order, OrderRequest, OrderStatus, Product, Side
from app.notify.base import LogNotifier
from app.storage.db import make_engine, make_sessionmaker
from app.storage.journal import (
    Journal,
    SqlJournalStore,
    daily_pnl_rows,
    fill_row,
    order_row,
)
from tests.builders import FUT, fill, ist
from tests.test_migrations import URL, _reachable

pytestmark = pytest.mark.integration
BACKEND = Path(__file__).resolve().parents[1]


async def test_sql_journal_roundtrip() -> None:
    if not URL or not await _reachable(URL):
        pytest.skip("TEST_DATABASE_URL not set or unreachable")
    env = {**os.environ, "DATABASE_URL": URL}
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        check=True,
    )
    engine = make_engine(URL)
    try:
        async with engine.begin() as conn:
            for t in ("fills", "order_events", "orders", "daily_pnl"):
                await conn.execute(text(f"DELETE FROM {t}"))  # noqa: S608 - fixed names
        store = SqlJournalStore(make_sessionmaker(engine))
        j = Journal(store, "paper", LogNotifier())
        await j.start()
        o = Order(
            OrderRequest("s1", FUT, Side.BUY, 75, product=Product.NRML),
            created_at=ist(10, 0),
            updated_at=ist(10, 0),
        )
        j.record_order(o)
        o.status, o.filled_quantity, o.average_price = OrderStatus.COMPLETE, 75, 100.0
        j.record_order(o)
        f = fill(Side.BUY, 75, 100.0, product=Product.NRML)
        f = type(f)(
            **{**{k: getattr(f, k) for k in f.__slots__}, "client_order_id": o.client_order_id}
        )
        j.record_fill(f)
        j.record_fill(f)  # duplicate fill id ignored
        assert await j.flush(10)
        assert j.failures == 0
        d = date(2025, 11, 3)
        fills = await store.load_fills("paper", d)
        assert [(r["quantity"], r["price"]) for r in fills] == [(75, 100.0)]
        assert await store.load_open_orders("paper", d) == []
        assert await store.load_order_ids("paper", d) == {o.client_order_id}
        assert len(await store.load_carry_fills("paper", date(2025, 11, 4))) == 1
        rows = daily_pnl_rows(
            d,
            "paper",
            {"__account__": {"gross": 1, "charges": 0, "net": 1, "trades": 2}},
            ist(15, 35),
        )
        await store.upsert_daily_pnl(rows)
        await store.upsert_daily_pnl(rows)  # idempotent upsert
        await j.stop()
        assert order_row(o, "paper")["status"] == "COMPLETE"
        assert fill_row(f, "paper")["trading_date"] == d
    finally:
        await engine.dispose()
