"""IST time helpers. All trading logic runs in Asia/Kolkata time."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(ts: datetime) -> datetime:
    """Convert an aware datetime to IST. Naive datetimes are assumed to already be IST
    (Kite returns naive IST timestamps in several payloads)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def ist_datetime(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def next_occurrence(now: datetime, at: time) -> datetime:
    """Next IST datetime strictly after `now` whose wall-clock time is `at`."""
    now = to_ist(now)
    candidate = ist_datetime(now.date(), at)
    if candidate <= now:
        candidate = ist_datetime(now.date() + timedelta(days=1), at)
    return candidate


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return now_ist()


class ManualClock:
    """A clock driven explicitly (tests, backtests, and the engine's market time)."""

    def __init__(self, start: datetime) -> None:
        self._now = to_ist(start)

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        self._now = to_ist(ts)

    def advance(self, delta: timedelta) -> None:
        self._now += delta
