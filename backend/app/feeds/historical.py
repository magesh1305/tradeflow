"""Replays stored candles as ticks, through the same engine used for paper and live.

Each candle becomes 4 ticks inside its own interval:
  open at t, then the extremes, then close at t + interval - 1s.
  Up candle (close >= open):  O -> L -> H -> C  (dips first, then rallies)
  Down candle (close < open): O -> H -> L -> C
Cumulative day volume steps up at each close tick. Tradables get a synthetic quote
of +/- `spread_ticks` ticks around the price, so liquidity rules and paper fills
behave sensibly. Indices get no bid/ask, as on Kite.

Tokens subscribed mid-day (e.g. options chosen at 09:20) are loaded on demand from
that moment on. Missing or thin data is recorded in `missing`, never silently
skipped: a backtest result is only as good as its data.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.core.models import Candle, Instrument, InstrumentType, Tick
from app.data.historical import HistoryStore, interval_name, session_bounds
from app.feeds.base import FeedSink

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MissingData:
    day: date
    token: int
    symbol: str
    expected: int
    found: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "token": self.token,
            "symbol": self.symbol,
            "expected_candles": self.expected,
            "found_candles": self.found,
        }


def candle_ticks(c: Candle, inst: Instrument | None, spread_ticks: int) -> list[Tick]:
    step = timedelta(seconds=c.interval / 3)
    up = c.close >= c.open
    path = [c.open, c.low, c.high, c.close] if up else [c.open, c.high, c.low, c.close]
    times = [c.ts, c.ts + step, c.ts + 2 * step, c.ts + timedelta(seconds=c.interval - 1)]
    tradable = inst is not None and inst.instrument_type is not InstrumentType.INDEX
    tick_size = inst.tick_size if inst else 0.05
    out = []
    for px, ts in zip(path, times, strict=True):
        bid = ask = None
        if tradable:
            bid = max(round(px - spread_ticks * tick_size, 2), tick_size)
            ask = round(px + spread_ticks * tick_size, 2)
        out.append(
            Tick(
                c.token,
                ts,
                px,
                oi=c.oi,
                bid=bid,
                ask=ask,
                bid_qty=10**6 if tradable else 0,
                ask_qty=10**6 if tradable else 0,
            )
        )
    return out


class HistoricalFeed:
    def __init__(
        self,
        store: HistoryStore,
        *,
        interval: int = 60,
        instrument: Callable[[int], Instrument | None] = lambda _t: None,
        spread_ticks: int = 1,
        min_coverage: float = 0.9,
    ) -> None:
        interval_name(interval)  # validate
        self._store = store
        self._interval = interval
        self._instrument = instrument
        self._spread = spread_ticks
        self._min_coverage = min_coverage
        self._sink: FeedSink | None = None
        self._tokens: set[int] = set()
        self._loaded: set[int] = set()
        self._connected = False
        self.missing: list[MissingData] = []
        self.ticks_emitted = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self, sink: FeedSink) -> None:
        self._sink = sink
        self._connected = True
        sink.on_feed_status(True, "historical replay")

    async def stop(self) -> None:
        self._connected = False

    def subscribe(self, tokens: Iterable[int]) -> None:
        self._tokens.update(tokens)

    def unsubscribe(self, tokens: Iterable[int]) -> None:
        self._tokens.difference_update(tokens)

    def expected_candles(self) -> int:
        open_, close = session_bounds(date(2000, 1, 3))
        return int((close - open_).total_seconds() // self._interval)

    async def _load(
        self,
        token: int,
        day: date,
        start: datetime,
        end: datetime,
        *,
        heap: list[Any],
        seq: itertools.count[int],
    ) -> None:
        self._loaded.add(token)
        open_, close = session_bounds(day)
        # Load from the open even when subscribing mid-day: Kite's ticks carry the
        # cumulative DAY volume, so earlier candles count toward the starting volume.
        day_candles = await self._store.load_candles(token, self._interval, open_, end)
        candles = [c for c in day_candles if c.ts >= start]
        base_volume = sum(c.volume for c in day_candles if c.ts < start)
        full_span = (close - open_).total_seconds()
        expected = int(self.expected_candles() * (end - start).total_seconds() / full_span)
        if len(candles) < expected * self._min_coverage:
            inst = self._instrument(token)
            gap = MissingData(
                day, token, inst.tradingsymbol if inst else str(token), expected, len(candles)
            )
            self.missing.append(gap)
            log.warning("missing historical data", extra=gap.as_dict())
        inst = self._instrument(token)
        volume = base_volume
        for c in candles:
            for i, t in enumerate(candle_ticks(c, inst, self._spread)):
                if i == 3:
                    volume += c.volume
                t.volume = volume
                heapq.heappush(heap, (t.ts, next(seq), t))

    async def play_day(self, day: date, pace: Callable[[], Awaitable[None]]) -> None:
        """Emit the day's ticks in time order. `pace` is awaited after every batch
        (the runner passes engine.drain, so the engine keeps exact step with the feed)."""
        if self._sink is None:
            raise RuntimeError("feed not started")
        start, end = session_bounds(day)
        heap: list[Any] = []
        seq = itertools.count()
        self._loaded = set()
        for token in sorted(self._tokens):
            await self._load(token, day, start, end, heap=heap, seq=seq)
        while heap:
            ts = heap[0][0]
            batch = []
            while heap and heap[0][0] == ts:
                batch.append(heapq.heappop(heap)[2])
            batch = [t for t in batch if t.token in self._tokens]
            if batch:
                self._sink.on_ticks(batch)
                self.ticks_emitted += len(batch)
                await pace()
            new = self._tokens - self._loaded
            for token in sorted(new):  # subscribed during the day: load from now on
                await self._load(token, day, ts + timedelta(seconds=1), end, heap=heap, seq=seq)
