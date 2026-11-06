"""Historical candles and instrument history: storage, chunked download, recording.

- Kite's historical API limits how many days one request may cover, depending on
  the interval (HISTORY_CHUNK_DAYS; not stated in the docs, so verify). The
  downloader splits ranges into chunks and stays under 3 req/sec through
  KiteBroker's historical limiter. A failed chunk is reported, not silently skipped.
- Candles are upserted into `candles` (unique on token, interval, ts).
- Expired contracts can't be fetched later, so a daily job records 1-minute
  candles for strikes around ATM, plus the instrument definitions they belong to.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.brokers.kite import BrokerError, KiteBroker
from app.core.clock import IST, to_ist
from app.core.models import Candle, Exchange, Instrument, InstrumentType
from app.data.instruments import InstrumentNotFoundError, InstrumentStore
from app.storage.models import CandleRow, InstrumentRow

log = logging.getLogger(__name__)

INTERVAL_SECONDS: dict[str, int] = {
    "minute": 60,
    "3minute": 180,
    "5minute": 300,
    "10minute": 600,
    "15minute": 900,
    "30minute": 1800,
    "60minute": 3600,
    "day": 86400,
}
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def interval_name(seconds: int) -> str:
    for name, secs in INTERVAL_SECONDS.items():
        if secs == seconds:
            return name
    raise ValueError(f"no Kite interval of {seconds}s")


# --- storage ------------------------------------------------------------------------


class HistoryStore(Protocol):
    async def upsert_candles(self, candles: Sequence[Candle]) -> int: ...
    async def load_candles(
        self, token: int, interval: int, start: datetime, end: datetime
    ) -> list[Candle]: ...
    async def upsert_instruments(self, instruments: Iterable[Instrument], seen: date) -> int: ...
    async def load_instruments(self, start: date, end: date) -> list[tuple[Instrument, date]]:
        """Instruments live at some point in [start, end], with the date first seen."""


def _inst_from_row(r: Any) -> Instrument:
    return Instrument(
        token=int(r.instrument_token),
        tradingsymbol=r.tradingsymbol,
        exchange=Exchange(r.exchange),
        name=r.name,
        instrument_type=InstrumentType(r.instrument_type),
        lot_size=int(r.lot_size),
        tick_size=float(r.tick_size),
        expiry=r.expiry,
        strike=r.strike,
    )


class SqlHistoryStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert_candles(self, candles: Sequence[Candle]) -> int:
        if not candles:
            return 0
        rows = [
            {
                "instrument_token": c.token,
                "interval": interval_name(c.interval),
                "ts": c.ts,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "oi": c.oi,
            }
            for c in candles
        ]
        async with self._sessions.begin() as s:
            for i in range(0, len(rows), 1000):
                stmt = insert(CandleRow).values(rows[i : i + 1000])
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["instrument_token", "interval", "ts"],
                        set_={
                            k: stmt.excluded[k]
                            for k in ("open", "high", "low", "close", "volume", "oi")
                        },
                    )
                )
        return len(rows)

    async def load_candles(
        self, token: int, interval: int, start: datetime, end: datetime
    ) -> list[Candle]:
        t = CandleRow.__table__
        stmt = (
            select(t)
            .where(
                t.c.instrument_token == token,
                t.c.interval == interval_name(interval),
                t.c.ts >= start,
                t.c.ts < end,
            )
            .order_by(t.c.ts)
        )
        async with self._sessions() as s:
            result = await s.execute(stmt)
            return [
                Candle(
                    r.instrument_token,
                    interval,
                    to_ist(r.ts),
                    r.open,
                    r.high,
                    r.low,
                    r.close,
                    int(r.volume),
                    int(r.oi),
                )
                for r in result
            ]

    async def upsert_instruments(self, instruments: Iterable[Instrument], seen: date) -> int:
        rows = [
            {
                "exchange": str(i.exchange),
                "tradingsymbol": i.tradingsymbol,
                "instrument_token": i.token,
                "name": i.name,
                "instrument_type": str(i.instrument_type),
                "lot_size": i.lot_size,
                "tick_size": i.tick_size,
                "expiry": i.expiry,
                "strike": i.strike,
                "first_seen": seen,
                "last_seen": seen,
            }
            for i in instruments
        ]
        if not rows:
            return 0
        t = InstrumentRow.__table__
        async with self._sessions.begin() as s:
            for i in range(0, len(rows), 1000):
                stmt = insert(InstrumentRow).values(rows[i : i + 1000])
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["exchange", "tradingsymbol"],
                        set_={
                            "instrument_token": stmt.excluded.instrument_token,
                            "lot_size": stmt.excluded.lot_size,
                            "tick_size": stmt.excluded.tick_size,
                            # earliest and latest sightings, in any upsert order
                            "last_seen": func.greatest(t.c.last_seen, stmt.excluded.last_seen),
                            "first_seen": func.least(t.c.first_seen, stmt.excluded.first_seen),
                        },
                    )
                )
        return len(rows)

    async def load_instruments(self, start: date, end: date) -> list[tuple[Instrument, date]]:
        t = InstrumentRow.__table__
        stmt = select(t).where(
            t.c.first_seen <= end, (t.c.expiry.is_(None)) | (t.c.expiry >= start)
        )
        async with self._sessions() as s:
            result = await s.execute(stmt)
            return [(_inst_from_row(r), r.first_seen) for r in result]


@dataclass
class MemoryHistoryStore:
    candles: dict[tuple[int, int], dict[datetime, Candle]] = field(default_factory=dict)
    instruments: dict[str, tuple[Instrument, date]] = field(default_factory=dict)

    async def upsert_candles(self, candles: Sequence[Candle]) -> int:
        for c in candles:
            self.candles.setdefault((c.token, c.interval), {})[c.ts] = c
        return len(candles)

    async def load_candles(
        self, token: int, interval: int, start: datetime, end: datetime
    ) -> list[Candle]:
        series = self.candles.get((token, interval), {})
        return [series[ts] for ts in sorted(series) if start <= ts < end]

    async def upsert_instruments(self, instruments: Iterable[Instrument], seen: date) -> int:
        n = 0
        for i in instruments:
            prev = self.instruments.get(i.key)
            self.instruments[i.key] = (i, min(prev[1], seen) if prev else seen)
            n += 1
        return n

    async def load_instruments(self, start: date, end: date) -> list[tuple[Instrument, date]]:
        return [
            (i, first)
            for i, first in self.instruments.values()
            if first <= end and (i.expiry is None or i.expiry >= start)
        ]


# --- download --------------------------------------------------------------------------


def chunk_ranges(start: datetime, end: datetime, max_days: int) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into consecutive ranges of at most `max_days` days."""
    if end < start:
        raise ValueError("end before start")
    out = []
    cur = start
    step = timedelta(days=max_days)
    while cur <= end:
        nxt = min(cur + step - timedelta(seconds=1), end)
        out.append((cur, nxt))
        cur = nxt + timedelta(seconds=1)
    return out


@dataclass
class DownloadResult:
    instrument: str
    candles: int = 0
    chunks: int = 0
    failed_chunks: list[str] = field(default_factory=list)


class HistoryDownloader:
    def __init__(self, broker: KiteBroker, store: HistoryStore, chunk_days: dict[str, int]) -> None:
        self._broker = broker
        self._store = store
        self._chunk_days = chunk_days

    async def download(
        self, inst: Instrument, interval: str, start: datetime, end: datetime, *, oi: bool = True
    ) -> DownloadResult:
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"unknown interval {interval!r}; use one of {list(INTERVAL_SECONDS)}")
        secs = INTERVAL_SECONDS[interval]
        result = DownloadResult(inst.key)
        for a, b in chunk_ranges(start, end, self._chunk_days.get(interval, 60)):
            result.chunks += 1
            try:
                rows = await self._broker.historical_data(
                    inst.token,
                    a,
                    b,
                    interval,
                    oi=oi and inst.instrument_type is not InstrumentType.INDEX,
                )
            except BrokerError as exc:
                msg = f"{a:%Y-%m-%d}..{b:%Y-%m-%d}: {exc}"
                result.failed_chunks.append(msg)
                log.error("history chunk failed", extra={"instrument": inst.key, "error": msg})
                continue
            candles = [
                Candle(
                    inst.token,
                    secs,
                    to_ist(r["date"]),
                    float(r["open"]),
                    float(r["high"]),
                    float(r["low"]),
                    float(r["close"]),
                    int(r.get("volume") or 0),
                    int(r.get("oi") or 0),
                )
                for r in rows
            ]
            result.candles += await self._store.upsert_candles(candles)
        await self._store.upsert_instruments([inst], start.date())
        return result


def recording_targets(
    store: InstrumentStore,
    underlying: str,
    spot: float,
    today: date,
    *,
    width: int,
    expiries: int,
) -> list[Instrument]:
    """Spot, nearest future and `width` strikes either side of ATM for the nearest
    `expiries` option expiries (calls and puts)."""
    out: list[Instrument] = []
    with contextlib.suppress(InstrumentNotFoundError):
        out.append(store.spot(underlying))
    with contextlib.suppress(InstrumentNotFoundError):
        out.append(store.nearest_future(underlying, today))
    for exp in [e for e in store.expiries(underlying) if e >= today][:expiries]:
        strikes = store.strikes(underlying, exp)
        if not strikes:
            continue
        atm = store.atm_strike(underlying, exp, spot)
        i = strikes.index(atm)
        for strike in strikes[max(i - width, 0) : i + width + 1]:
            for ot in ("CE", "PE"):
                with contextlib.suppress(InstrumentNotFoundError):
                    out.append(store.option(underlying, exp, strike, ot))
    return out


def session_bounds(day: date) -> tuple[datetime, datetime]:
    return (datetime.combine(day, MARKET_OPEN, IST), datetime.combine(day, MARKET_CLOSE, IST))


async def record_underlying(
    downloader: HistoryDownloader,
    history: HistoryStore,
    master: InstrumentStore,
    underlying: str,
    *,
    start: datetime,
    end: datetime,
    interval: str = "minute",
    width: int = 10,
    expiries: int = 2,
) -> list[DownloadResult]:
    """Download the spot first (its last close picks ATM), then the nearest future and
    `width` strikes either side of ATM for the nearest `expiries` expiries."""
    spot_inst = master.spot(underlying)
    results = [await downloader.download(spot_inst, interval, start, end)]
    candles = await history.load_candles(spot_inst.token, INTERVAL_SECONDS[interval], start, end)
    if not candles:
        results[0].failed_chunks.append("no spot candles: cannot pick strikes")
        return results
    targets = recording_targets(
        master, underlying, candles[-1].close, end.date(), width=width, expiries=expiries
    )
    for inst in targets:
        if inst.token != spot_inst.token:
            results.append(await downloader.download(inst, interval, start, end))
    return results
