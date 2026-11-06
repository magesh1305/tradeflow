from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from app.core.clock import IST
from app.core.models import InstrumentType, Tick
from app.data.instruments import InstrumentStore
from app.feeds.kite import MAX_TOKENS_PER_CONNECTION, KiteFeed, tick_from_kite
from app.feeds.random_walk import RandomWalkFeed, bs_price, monthly_expiry, synthetic_nifty


class RecordingSink:
    def __init__(self) -> None:
        self.ticks: list[Tick] = []
        self.orders: list[dict[str, Any]] = []
        self.status: list[tuple[bool, str]] = []
        self.threads: set[int] = set()

    def on_ticks(self, ticks: list[Tick]) -> None:
        self.threads.add(threading.get_ident())
        self.ticks.extend(ticks)

    def on_order_update(self, payload: dict[str, Any]) -> None:
        self.threads.add(threading.get_ident())
        self.orders.append(payload)

    def on_feed_status(self, connected: bool, detail: str) -> None:
        self.status.append((connected, detail))


class FakeTicker:
    """Stands in for KiteTicker; records calls, never opens a socket."""

    def __init__(self) -> None:
        self.subscribed: list[list[int]] = []
        self.modes: list[tuple[str, list[int]]] = []
        self.unsubscribed: list[list[int]] = []
        self.connected_threaded: bool | None = None
        self.closed = False
        self.on_ticks: Any = None
        self.on_connect: Any = None
        self.on_close: Any = None
        self.on_error: Any = None
        self.on_reconnect: Any = None
        self.on_noreconnect: Any = None
        self.on_order_update: Any = None

    def connect(self, threaded: bool = False) -> None:
        self.connected_threaded = threaded

    def subscribe(self, tokens: list[int]) -> None:
        self.subscribed.append(tokens)

    def set_mode(self, mode: str, tokens: list[int]) -> None:
        self.modes.append((mode, tokens))

    def unsubscribe(self, tokens: list[int]) -> None:
        self.unsubscribed.append(tokens)

    def stop_retry(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def make_feed() -> tuple[KiteFeed, FakeTicker, list[Any]]:
    ticker = FakeTicker()
    reactor_calls: list[Any] = []

    def call_in_reactor(fn: Any, *args: Any) -> None:
        reactor_calls.append(fn)
        fn(*args)

    feed = KiteFeed(
        "key",
        "token",
        ticker_factory=lambda k, t: ticker,
        call_in_reactor=call_in_reactor,
        enforce_single_start=False,
    )
    return feed, ticker, reactor_calls


RAW_TICK = {
    "instrument_token": 2001,
    "mode": "full",
    "last_price": 101.5,
    "volume_traded": 12510,
    "oi": 21845,
    "exchange_timestamp": datetime(2025, 11, 3, 10, 0, 1),
    "last_trade_time": datetime(2025, 11, 3, 10, 0, 0),
    "depth": {
        "buy": [{"price": 101.45, "quantity": 150, "orders": 2}],
        "sell": [{"price": 101.55, "quantity": 75, "orders": 1}],
    },
}


def test_tick_conversion_full_mode() -> None:
    t = tick_from_kite(RAW_TICK)
    assert (t.token, t.ltp, t.volume, t.oi) == (2001, 101.5, 12510, 21845)
    assert (t.bid, t.ask, t.bid_qty, t.ask_qty) == (101.45, 101.55, 150, 75)
    assert t.ts == datetime(2025, 11, 3, 10, 0, 1, tzinfo=IST)


def test_tick_conversion_ltp_mode_and_empty_depth() -> None:
    t = tick_from_kite(
        {
            "instrument_token": 1,
            "last_price": 5.0,
            "depth": {"buy": [{"price": 0, "quantity": 0}], "sell": []},
        }
    )
    assert t.bid is None and t.ask is None
    assert t.ts.tzinfo is not None


async def test_kite_feed_bridges_reactor_thread_to_loop() -> None:
    feed, ticker, _ = make_feed()
    sink = RecordingSink()
    feed.subscribe([2001, 2002])  # before connect: queued
    await feed.start(sink)
    assert ticker.connected_threaded is True
    assert ticker.subscribed == []

    loop_thread = threading.get_ident()

    def reactor_thread() -> None:
        ticker.on_connect(ticker, {})
        ticker.on_ticks(ticker, [RAW_TICK, {"bad": "tick"}])
        ticker.on_order_update(ticker, {"order_id": "1", "status": "COMPLETE"})

    th = threading.Thread(target=reactor_thread)
    th.start()
    th.join()
    for _ in range(5):
        await asyncio.sleep(0)

    assert sorted(ticker.subscribed[0]) == [2001, 2002]
    assert ticker.modes[0][0] == "full"
    assert [t.token for t in sink.ticks] == [2001]  # bad tick dropped
    assert sink.orders == [{"order_id": "1", "status": "COMPLETE"}]
    assert sink.threads == {loop_thread}  # sink only ever runs on the loop thread
    assert sink.status[0] == (True, "connected")
    assert feed.connected


async def test_kite_feed_dynamic_subscribe_goes_through_reactor() -> None:
    feed, ticker, reactor_calls = make_feed()
    await feed.start(RecordingSink())
    ticker.on_connect(ticker, {})
    reactor_calls.clear()
    feed.subscribe([7, 8])
    feed.subscribe([7])  # duplicate ignored
    assert ticker.subscribed[-1] == [7, 8]
    assert len(reactor_calls) == 1
    feed.unsubscribe([8])
    assert ticker.unsubscribed == [[8]]
    assert feed.subscribed == {7}


async def test_kite_feed_reconnect_and_close_status() -> None:
    feed, ticker, _ = make_feed()
    sink = RecordingSink()
    await feed.start(sink)
    ticker.on_connect(ticker, {})
    ticker.on_close(ticker, 1006, "abnormal")
    ticker.on_reconnect(ticker, 2)
    ticker.on_noreconnect(ticker)
    await asyncio.sleep(0)
    assert not feed.connected
    assert [s[0] for s in sink.status] == [True, False, False, False]
    assert "attempt 2" in sink.status[2][1]
    await feed.stop()
    assert ticker.closed


async def test_kite_feed_token_limit() -> None:
    feed, _, _ = make_feed()
    feed.subscribe(range(MAX_TOKENS_PER_CONNECTION))
    with pytest.raises(ValueError, match="3000"):
        feed.subscribe([10**9])


async def test_kite_feed_single_start_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.feeds.kite as kite_mod  # noqa: PLC0415

    monkeypatch.setattr(kite_mod, "_started_once", False)
    ticker = FakeTicker()
    a = KiteFeed("k", "t", ticker_factory=lambda k, t: ticker, call_in_reactor=lambda *a: None)
    b = KiteFeed("k", "t", ticker_factory=lambda k, t: ticker, call_in_reactor=lambda *a: None)
    await a.start(RecordingSink())
    with pytest.raises(RuntimeError, match="once per process"):
        await b.start(RecordingSink())


# --- random walk ---------------------------------------------------------------

TODAY = date(2025, 11, 3)  # Monday


def test_synthetic_universe() -> None:
    u = synthetic_nifty(TODAY)
    assert u.index.instrument_type is InstrumentType.INDEX
    assert u.future.expiry == date(2025, 11, 25)  # last Tuesday
    assert {o.expiry for o in u.options} == {date(2025, 11, 4)}  # next Tuesday
    assert len(u.options) == 82
    tokens = [i.token for i in u.all]
    assert len(set(tokens)) == len(tokens)
    store = InstrumentStore()
    store.load(u.all, TODAY)
    assert store.atm_strike("NIFTY", date(2025, 11, 4), 24010) == 24000
    assert store.spot("NIFTY") is u.index
    assert store.nearest_future("NIFTY", TODAY) is u.future


def test_monthly_expiry_rolls_after_last_tuesday() -> None:
    assert monthly_expiry(date(2025, 11, 26)) == date(2025, 12, 30)
    assert monthly_expiry(date(2025, 12, 31)) == date(2026, 1, 27)


def test_bs_put_call_parity() -> None:
    c = bs_price(24000, 24100, 7 / 365, 0.15, True)
    p = bs_price(24000, 24100, 7 / 365, 0.15, False)
    assert c - p == pytest.approx(24000 - 24100, abs=1e-6)
    assert bs_price(24000, 23000, 0, 0.15, True) == 1000


async def test_random_walk_simulated_day_is_deterministic() -> None:
    u = synthetic_nifty(TODAY)
    start = datetime(2025, 11, 3, 9, 15, tzinfo=IST)

    async def run() -> RecordingSink:
        feed = RandomWalkFeed(
            u, realtime=False, start=start, end=start + timedelta(minutes=10), seed=7
        )
        sink = RecordingSink()
        feed.subscribe([u.index.token, u.future.token, u.options[40].token])
        await feed.start(sink)
        await asyncio.wait_for(feed.finished.wait(), 5)
        return sink

    a, b = await run(), await run()
    assert len(a.ticks) == 601 * 3
    assert [t.ltp for t in a.ticks] == [t.ltp for t in b.ticks]
    assert a.status[-1] == (False, "sim feed stopped")
    for t in a.ticks:
        assert t.ltp > 0
        if t.token != u.index.token:
            assert t.bid is not None and t.ask is not None and t.bid < t.ask
    assert a.ticks[0].ts == start
    assert a.ticks[-1].ts == start + timedelta(minutes=10)


async def test_random_walk_rejects_unknown_token_and_stops() -> None:
    u = synthetic_nifty(TODAY)
    feed = RandomWalkFeed(u, step=timedelta(milliseconds=1), seed=1)
    with pytest.raises(ValueError, match="unknown"):
        feed.subscribe([123])
    sink = RecordingSink()
    feed.subscribe([u.future.token])
    await feed.start(sink)
    await asyncio.sleep(0.05)
    await feed.stop()
    assert sink.ticks
    assert not feed.connected
