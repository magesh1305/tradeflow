"""Async sliding-window rate limiter with several windows (e.g. 8/sec AND 350/min).

Kite limits: orders 10/sec, 400/min, 5000/day; historical 3/sec; quote 1/sec;
everything else 10/sec. We configure limits strictly below these.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence


class RateLimiter:
    def __init__(
        self,
        limits: Sequence[tuple[int, float]],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """`limits` is a list of (max_calls, window_seconds)."""
        if not limits:
            raise ValueError("at least one limit is required")
        self._limits = [(int(n), float(w)) for n, w in limits]
        self._calls: deque[float] = deque()
        self._max_window = max(w for _, w in self._limits)
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    def _wait_time(self, now: float) -> float:
        while self._calls and now - self._calls[0] >= self._max_window:
            self._calls.popleft()
        wait = 0.0
        for max_calls, window in self._limits:
            in_window = [t for t in self._calls if now - t < window]
            if len(in_window) >= max_calls:
                # Wait until the oldest call that matters leaves this window.
                oldest = in_window[len(in_window) - max_calls]
                wait = max(wait, window - (now - oldest))
        return wait

    async def acquire(self) -> None:
        async with self._lock:  # FIFO: callers are served in arrival order
            while True:
                now = self._clock()
                wait = self._wait_time(now)
                if wait <= 0:
                    self._calls.append(now)
                    return
                await self._sleep(wait)
