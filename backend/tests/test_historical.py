"""Chunked downloads, storage, candle-to-tick replay and missing-data reporting."""

from __future__ import annotations

import asyncio
import itertools
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from kiteconnect import KiteConnect
from kiteconnect.exceptions import NetworkException

from app.brokers.kite import KiteBroker
from app.core.clock import IST
from app.core.models import Candle, Tick
from app.data.historical import (
    HistoryDownloader,
    MemoryHistoryStore,
    chunk_ranges,
    interval_name,
    record_underlying,
)
from app.data.instruments import InstrumentStore
from app.feeds.historical import HistoricalFeed, candle_ticks
from app.feeds.random_walk import synthetic_nifty
from tests.builders import CE, FUT
from tests.test_live_executor import FastLimiter

DAY = date(2025, 11, 3)


def at(h: int, m: int, d: date = DAY) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def test_chunk_ranges_cover_range_without_overlap() -> None:
    start, end = at(9, 15), at(15, 30, date(2026, 2, 10))
    chunks = chunk_ranges(start, end, 60)
    assert chunks[0][0] == start and chunks[-1][1] == end
    for (_, b), (a2, _) in itertools.pairwise(chunks):
        assert a2 == b + timedelta(seconds=1)
    assert all((b - a) < timedelta(days=60) for a, b in chunks)
    assert len(chunk_ranges(start, start, 60)) == 1
    with pytest.raises(ValueError):
        chunk_ranges(end, start, 60)


def test_interval_names() -> None:
    assert interval_name(60) == "minute"
    assert interval_name(300) == "5minute"
    with pytest.raises(ValueError):
        interval_name(7)


def kite_rows(start: datetime, n: int, base: float = 100.0) -> list[dict[str, Any]]:
    return [
        {
            "date": (start + timedelta(minutes=i)).replace(tzinfo=None),
            "open": base + i,
            "high": base + i + 1,
            "low": base + i - 1,
            "close": base + i + 0.5,
            "volume": 1000,
            "oi": 50,
        }
        for i in range(n)
    ]


async def test_downloader_chunks_upserts_and_reports_failures() -> None:
    kite = MagicMock(spec=KiteConnect)
    calls: list[tuple[Any, ...]] = []

    def historical(token: int, a: datetime, b: datetime, interval: str, **kw: Any) -> Any:
        calls.append((a, b, interval, kw))
        if len(calls) == 2:
            raise NetworkException("timeout")
        return kite_rows(a.replace(tzinfo=IST), 3)

    kite.historical_data.side_effect = historical
    broker = KiteBroker(kite, orders_enabled=False, historical_limiter=FastLimiter())
    store = MemoryHistoryStore()
    dl = HistoryDownloader(broker, store, {"minute": 60})
    res = await dl.download(FUT, "minute", at(9, 15), at(15, 30, date(2026, 2, 10)))
    assert res.chunks == len(calls) == 2  # ~100 days in 60-day chunks
    assert len(res.failed_chunks) == 1 and "timeout" in res.failed_chunks[0]
    assert res.candles == 3 * (res.chunks - 1)
    assert calls[0][3] == {"continuous": False, "oi": True}
    assert calls[0][0].tzinfo is None  # SDK gets naive IST
    stored = await store.load_candles(FUT.token, 60, at(0, 0), at(23, 0, date(2027, 1, 1)))
    assert len(stored) == res.candles
    assert stored[0].ts.tzinfo is not None
    # Re-downloading upserts instead of duplicating.
    kite.historical_data.side_effect = lambda *a, **k: kite_rows(at(9, 15), 3)
    await dl.download(FUT, "minute", at(9, 15), at(15, 30))
    assert len(await store.load_candles(FUT.token, 60, at(9, 15), at(9, 18))) == 3
    assert FUT.key in store.instruments


async def test_record_underlying_picks_strikes_from_spot_close() -> None:
    u = synthetic_nifty(DAY)
    master = InstrumentStore()
    master.load(u.all, DAY)
    kite = MagicMock(spec=KiteConnect)
    kite.historical_data.side_effect = lambda token, a, b, interval, **k: kite_rows(
        at(9, 15), 2, base=24_010.0 if token == u.index.token else 50.0
    )
    broker = KiteBroker(kite, orders_enabled=False, historical_limiter=FastLimiter())
    store = MemoryHistoryStore()
    res = await record_underlying(
        HistoryDownloader(broker, store, {"minute": 60}),
        store,
        master,
        "NIFTY",
        start=at(9, 15),
        end=at(15, 30),
        width=2,
        expiries=1,
    )
    symbols = [r.instrument for r in res]
    assert symbols[0] == "NSE:NIFTY 50"
    assert any(s.endswith("FUT") for s in symbols)
    assert len([s for s in symbols if s.endswith(("CE", "PE"))]) == 10  # 5 strikes x 2


def test_candle_ticks_realistic_order() -> None:
    up = Candle(FUT.token, 60, at(9, 15), 100, 105, 98, 104)
    assert [t.ltp for t in candle_ticks(up, FUT, 1)] == [100, 98, 105, 104]  # O L H C
    down = Candle(FUT.token, 60, at(9, 15), 100, 105, 98, 99)
    ticks = candle_ticks(down, FUT, 1)
    assert [t.ltp for t in ticks] == [100, 105, 98, 99]  # O H L C
    assert ticks[0].ts == at(9, 15) and ticks[-1].ts == at(9, 15) + timedelta(seconds=59)
    assert ticks[0].bid == pytest.approx(99.9) and ticks[0].ask == pytest.approx(100.1)


class Sink:
    def __init__(self) -> None:
        self.ticks: list[Tick] = []

    def on_ticks(self, ticks: list[Tick]) -> None:
        self.ticks.extend(ticks)

    def on_order_update(self, payload: dict[str, Any]) -> None: ...
    def on_feed_status(self, connected: bool, detail: str) -> None: ...


def minute_candles(token: int, start: datetime, n: int, price: float = 100.0) -> list[Candle]:
    return [
        Candle(
            token, 60, start + timedelta(minutes=i), price, price + 1, price - 1, price, volume=10
        )
        for i in range(n)
    ]


async def test_feed_replays_in_time_order_and_reports_gaps() -> None:
    store = MemoryHistoryStore()
    await store.upsert_candles(minute_candles(FUT.token, at(9, 15), 375))
    await store.upsert_candles(minute_candles(CE.token, at(9, 15), 100))  # thin data
    lookup = {FUT.token: FUT, CE.token: CE}
    feed = HistoricalFeed(store, instrument=lookup.get)
    sink = Sink()
    await feed.start(sink)
    feed.subscribe([FUT.token, CE.token])
    await feed.play_day(DAY, lambda: asyncio.sleep(0))
    ts = [t.ts for t in sink.ticks]
    assert ts == sorted(ts)
    assert len([t for t in sink.ticks if t.token == FUT.token]) == 375 * 4
    assert sink.ticks[-1].volume > 0
    (gap,) = feed.missing
    assert (gap.symbol, gap.found, gap.expected) == (CE.tradingsymbol, 100, 375)


async def test_feed_loads_mid_day_subscriptions() -> None:
    store = MemoryHistoryStore()
    await store.upsert_candles(minute_candles(FUT.token, at(9, 15), 375))
    await store.upsert_candles(minute_candles(CE.token, at(9, 15), 375, price=50.0))
    feed = HistoricalFeed(store)
    sink = Sink()
    await feed.start(sink)
    feed.subscribe([FUT.token])

    async def pace() -> None:
        if sink.ticks and sink.ticks[-1].ts >= at(9, 20):
            feed.subscribe([CE.token])  # e.g. a strategy picks a strike at 09:20

    await feed.play_day(DAY, pace)
    ce = [t for t in sink.ticks if t.token == CE.token]
    assert ce and ce[0].ts > at(9, 20)
    assert ce[0].volume >= 5 * 10  # day volume so far (Kite ticks carry cumulative volume)
    assert feed.missing == []
