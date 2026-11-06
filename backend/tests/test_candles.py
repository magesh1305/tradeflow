from __future__ import annotations

from datetime import time

from app.engine.candles import CandleAggregator, bucket_start
from tests.builders import FUT, ist, tick


def test_bucket_alignment_to_market_open() -> None:
    anchor = time(9, 15)
    assert bucket_start(ist(9, 15), 300, anchor) == ist(9, 15)
    assert bucket_start(ist(9, 19, 59), 300, anchor) == ist(9, 15)
    assert bucket_start(ist(9, 20), 300, anchor) == ist(9, 20)
    assert bucket_start(ist(10, 14), 3600, anchor) == ist(9, 15)
    assert bucket_start(ist(10, 15), 3600, anchor) == ist(10, 15)
    assert bucket_start(ist(9, 0, 30), 60, anchor) == ist(9, 0)  # pre-open


def test_builds_ohlcv_and_emits_on_next_bucket() -> None:
    agg = CandleAggregator()
    agg.add(FUT.token, 60)
    seq = [
        (9, 15, 0, 100.0, 1000),
        (9, 15, 20, 103.0, 1100),
        (9, 15, 40, 99.0, 1250),
        (9, 15, 59, 101.0, 1300),
    ]
    for h, m, s, px, vol in seq:
        assert agg.on_tick(tick(FUT, px, ist(h, m, s), volume=vol)) == []
    (c,) = agg.on_tick(tick(FUT, 102.0, ist(9, 16, 1), volume=1400))
    assert (c.ts, c.open, c.high, c.low, c.close) == (ist(9, 15), 100.0, 103.0, 99.0, 101.0)
    assert c.volume == 300  # first tick has no previous volume, then +100 +150 +50
    assert c.interval == 60


def test_multiple_intervals_and_unsubscribed_tokens() -> None:
    agg = CandleAggregator()
    agg.add(FUT.token, 60)
    agg.add(FUT.token, 300)
    out = []
    t = ist(9, 15)
    for i in range(0, 6 * 60 + 1, 30):
        out += agg.on_tick(tick(FUT, 100.0 + i, t.replace(minute=15 + i // 60, second=i % 60)))
    assert sorted({c.interval for c in out}) == [60, 300]
    assert len([c for c in out if c.interval == 60]) == 6
    assert len([c for c in out if c.interval == 300]) == 1
    other = tick(FUT, 1.0, ist(9, 15))
    other.token = 42
    assert agg.on_tick(other) == []


def test_flush_closes_idle_candles() -> None:
    agg = CandleAggregator()
    agg.add(FUT.token, 60)
    agg.on_tick(tick(FUT, 100.0, ist(9, 15, 5)))
    assert agg.flush(ist(9, 15, 59)) == []
    (c,) = agg.flush(ist(9, 16))
    assert c.close == 100.0
    assert agg.flush(ist(9, 30)) == []


def test_late_tick_ignored() -> None:
    agg = CandleAggregator()
    agg.add(FUT.token, 60)
    agg.on_tick(tick(FUT, 100.0, ist(9, 15, 5)))
    agg.on_tick(tick(FUT, 101.0, ist(9, 16, 5)))
    assert agg.on_tick(tick(FUT, 500.0, ist(9, 15, 50))) == []
    (c,) = agg.flush(ist(9, 17))
    assert c.high == 101.0
