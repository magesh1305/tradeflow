"""Builds OHLCV candles from ticks, for the (token, interval) pairs strategies request.

- Buckets are aligned to the market open (09:15 IST), as Kite's candles are, so a
  60-minute candle runs 09:15-10:15 and so on.
- Candle volume is the change in the tick's cumulative day volume.
- A candle is emitted when the first tick of a later bucket arrives, or when
  `flush(now)` is called after the bucket's end (illiquid instruments, end of day).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta

from app.core.clock import to_ist
from app.core.models import Candle, Tick


def bucket_start(ts: datetime, interval: int, anchor: time) -> datetime:
    ts = to_ist(ts)
    origin = ts.replace(hour=anchor.hour, minute=anchor.minute, second=0, microsecond=0)
    if ts < origin:  # pre-open ticks: align to midnight instead
        origin = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    offset = int((ts - origin).total_seconds()) // interval * interval
    return origin + timedelta(seconds=offset)


class CandleAggregator:
    def __init__(self, anchor: time = time(9, 15)) -> None:
        self._anchor = anchor
        self._intervals: dict[int, set[int]] = defaultdict(set)  # token -> intervals
        self._open: dict[tuple[int, int], Candle] = {}
        self._last_volume: dict[int, int] = {}

    def add(self, token: int, interval: int) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive seconds")
        self._intervals[token].add(interval)

    def intervals(self, token: int) -> frozenset[int]:
        return frozenset(self._intervals.get(token, ()))

    def on_tick(self, tick: Tick) -> list[Candle]:
        intervals = self._intervals.get(tick.token)
        prev_vol = self._last_volume.get(tick.token)
        self._last_volume[tick.token] = tick.volume
        if not intervals:
            return []
        # Cumulative day volume; a drop means a new session, so count from zero.
        dvol = tick.volume - prev_vol if prev_vol is not None and tick.volume >= prev_vol else 0
        done: list[Candle] = []
        for interval in sorted(intervals):
            key = (tick.token, interval)
            start = bucket_start(tick.ts, interval, self._anchor)
            cur = self._open.get(key)
            if cur is not None and start > cur.ts:
                done.append(cur)
                cur = None
            elif cur is not None and start < cur.ts:
                continue  # late, out-of-order tick for a closed bucket: ignore
            if cur is None:
                self._open[key] = Candle(
                    token=tick.token,
                    interval=interval,
                    ts=start,
                    open=tick.ltp,
                    high=tick.ltp,
                    low=tick.ltp,
                    close=tick.ltp,
                    volume=dvol,
                    oi=tick.oi,
                )
            else:
                cur.high = max(cur.high, tick.ltp)
                cur.low = min(cur.low, tick.ltp)
                cur.close = tick.ltp
                cur.volume += dvol
                cur.oi = tick.oi
        return done

    def flush(self, now: datetime) -> list[Candle]:
        """Emit candles whose bucket has ended by `now`."""
        done: list[Candle] = []
        for key, c in list(self._open.items()):
            if c.ts + timedelta(seconds=c.interval) <= now:
                done.append(self._open.pop(key))
        return sorted(done, key=lambda c: (c.ts, c.token, c.interval))
