from __future__ import annotations

import asyncio
from typing import Any

from app.core.clock import ManualClock
from app.core.models import Order, OrderRequest, OrderStatus, Side
from app.data.instruments import InstrumentStore
from app.engine.engine import TradingEngine
from app.engine.positions import PositionBook
from app.execution.paper import PaperExecutor
from app.notify.base import LogNotifier
from app.risk.manager import RiskLimits, RiskManager
from app.storage.journal import Journal, MemoryJournalStore
from tests.builders import FUT, fill, ist, tick
from tests.test_engine import ManualFeed, Recorder, push


def journal(store: MemoryJournalStore, notifier: LogNotifier | None = None) -> Journal:
    return Journal(
        store,
        "paper",
        notifier or LogNotifier(),
        retry_initial=0.01,
        retry_max=0.02,
        alert_every=60,
    )


async def test_orders_and_fills_written_in_order() -> None:
    store = MemoryJournalStore()
    j = journal(store)
    await j.start()
    o = Order(OrderRequest("s1", FUT, Side.BUY, 75), created_at=ist(10, 0), updated_at=ist(10, 0))
    j.record_order(o)
    o.status = OrderStatus.COMPLETE
    o.filled_quantity = 75
    j.record_order(o)
    f = fill(Side.BUY, 75, 100.0)
    f = type(f)(**{**{k: getattr(f, k) for k in f.__slots__}, "client_order_id": o.client_order_id})
    j.record_fill(f)
    assert await j.flush(2)
    row = store.orders[o.client_order_id]
    assert row["status"] == "COMPLETE" and row["mode"] == "paper"
    assert [e["status"] for e in store.events] == ["PENDING", "COMPLETE"]  # snapshots
    assert list(store.fills) == [f.fill_id]
    await j.stop()


async def test_db_failure_never_blocks_and_alerts_once() -> None:
    store = MemoryJournalStore()
    store.fail_writes = 3
    notifier = LogNotifier()
    j = journal(store, notifier)
    await j.start()
    o = Order(OrderRequest("s1", FUT, Side.BUY, 75), created_at=ist(10, 0), updated_at=ist(10, 0))
    j.record_order(o)  # returns immediately even though the DB is down
    assert await j.flush(2)
    assert j.failures == 3
    assert o.client_order_id in store.orders  # eventually written
    critical = [m for lvl, m in notifier.sent if lvl == "critical"]
    assert len(critical) == 1 and "Journal DB write failing" in critical[0]
    await j.stop()


async def test_engine_journals_every_state_change_even_with_db_down() -> None:
    store = MemoryJournalStore()
    store.fail_writes = 2
    j = journal(store)
    await j.start()
    clock = ManualClock(ist(9, 15))
    instruments = InstrumentStore()
    instruments.load([FUT], clock.now().date())
    positions = PositionBook()
    engine = TradingEngine(
        instruments=instruments,
        feed=ManualFeed(),
        executor=PaperExecutor(clock, 0),
        risk=RiskManager(RiskLimits(), positions, clock),
        positions=positions,
        clock=clock,
        notifier=LogNotifier(),
        journal=j,
    )
    s = Recorder("s")
    engine.add_strategy(s)
    await engine.start(["s"])
    await push(engine, tick(FUT, 100, ist(9, 20)))
    o = s.buy(FUT, 75)
    engine._pump()
    assert engine.positions.net_quantity("s", FUT.token) == 75  # trading unaffected
    clock.set(ist(15, 12))
    rejected = s.buy(FUT, 75)
    engine._pump()
    assert await j.flush(2)
    statuses = [e["status"] for e in store.events if e["client_order_id"] == o.client_order_id]
    assert statuses[0] == "PENDING" and statuses[-1] == "COMPLETE"
    assert store.orders[rejected.client_order_id]["status"] == "REJECTED"
    (f,) = store.fills.values()
    assert (f["quantity"], f["price"], f["strategy_id"]) == (75, 100.0, "s")
    await engine.stop()
    await j.stop()


async def test_record_never_raises() -> None:
    j = journal(MemoryJournalStore())
    bad: Any = object()
    j.record_order(bad)  # malformed input is logged, not raised
    j.record_fill(bad)
    await asyncio.sleep(0)
    assert j.backlog == 0
