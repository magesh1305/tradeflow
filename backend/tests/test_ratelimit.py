from __future__ import annotations

import asyncio

import pytest

from app.core.ratelimit import RateLimiter


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_allows_burst_up_to_limit_then_waits() -> None:
    ft = FakeTime()
    rl = RateLimiter([(3, 1.0)], clock=ft.clock, sleep=ft.sleep)
    for _ in range(3):
        await rl.acquire()
    assert ft.sleeps == []
    await rl.acquire()
    assert ft.sleeps == [pytest.approx(1.0)]


async def test_multiple_windows_minute_limit_dominates() -> None:
    ft = FakeTime()
    rl = RateLimiter([(8, 1.0), (10, 60.0)], clock=ft.clock, sleep=ft.sleep)
    stamps = []
    for _ in range(11):
        await rl.acquire()
        stamps.append(ft.now)
    # Never more than 8 within any 1s window, never more than 10 within 60s.
    for i, t in enumerate(stamps):
        assert sum(1 for s in stamps if t - 1.0 < s <= t) <= 8
        assert sum(1 for s in stamps if t - 60.0 < s <= t) <= 10, i
    assert stamps[-1] >= 60.0


async def test_never_exceeds_kite_order_rate_under_concurrency() -> None:
    ft = FakeTime()
    rl = RateLimiter([(8, 1.0), (350, 60.0)], clock=ft.clock, sleep=ft.sleep)
    stamps: list[float] = []

    async def worker() -> None:
        await rl.acquire()
        stamps.append(ft.now)

    await asyncio.gather(*(worker() for _ in range(50)))
    stamps.sort()
    for t in stamps:
        assert sum(1 for s in stamps if t - 1.0 < s <= t) < 10


def test_requires_limits() -> None:
    with pytest.raises(ValueError, match="limit"):
        RateLimiter([])
