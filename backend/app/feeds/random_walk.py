"""Development feed: synthetic NIFTY index, future and weekly options on a random walk.

Lets the whole stack run without a Kite account (FEED=sim). The instruments are
synthetic (tokens >= 9_000_000 except the index), so they can never be confused
with tradable Kite contracts.

Two clock modes:
- realtime: one step per `step` of wall time, stamped with the current IST time;
- simulated: steps as fast as possible from `start` to `end` (tests, quick demos).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.core.clock import now_ist
from app.core.models import Exchange, Instrument, InstrumentType, Tick
from app.feeds.base import FeedSink

NIFTY_INDEX_TOKEN = 256265  # matches Kite's NSE:NIFTY 50 token
SYNTH_TOKEN_BASE = 9_000_000
TUESDAY = 1
TRADING_SECONDS_PER_YEAR = 252 * 6.25 * 3600


def _next_weekday(d: date, weekday: int) -> date:
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def monthly_expiry(today: date) -> date:
    exp = _last_weekday_of_month(today.year, today.month, TUESDAY)
    if exp < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        exp = _last_weekday_of_month(y, m, TUESDAY)
    return exp


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float, call: bool) -> float:
    """Black-Scholes with zero rates (good enough for synthetic quotes)."""
    if t_years <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    sd = vol * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if call:
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


@dataclass(frozen=True, slots=True)
class SyntheticUniverse:
    index: Instrument
    future: Instrument
    options: tuple[Instrument, ...]

    @property
    def all(self) -> list[Instrument]:
        return [self.index, self.future, *self.options]


def synthetic_nifty(
    today: date,
    *,
    spot: float = 24_000.0,
    strike_step: float = 50.0,
    strikes_each_side: int = 20,
    lot_size: int = 75,
) -> SyntheticUniverse:
    index = Instrument(
        token=NIFTY_INDEX_TOKEN,
        tradingsymbol="NIFTY 50",
        exchange=Exchange.NSE,
        name="NIFTY 50",
        instrument_type=InstrumentType.INDEX,
        lot_size=1,
        tick_size=0.05,
    )
    fut_exp = monthly_expiry(today)
    future = Instrument(
        token=SYNTH_TOKEN_BASE + 1,
        tradingsymbol=f"NIFTY{fut_exp:%y%b}FUT".upper(),
        exchange=Exchange.NFO,
        name="NIFTY",
        instrument_type=InstrumentType.FUT,
        lot_size=lot_size,
        tick_size=0.1,
        expiry=fut_exp,
    )
    opt_exp = _next_weekday(today, TUESDAY)
    atm = round(spot / strike_step) * strike_step
    options: list[Instrument] = []
    token = SYNTH_TOKEN_BASE + 100
    for i in range(-strikes_each_side, strikes_each_side + 1):
        strike = atm + i * strike_step
        for itype in (InstrumentType.CE, InstrumentType.PE):
            options.append(
                Instrument(
                    token=token,
                    tradingsymbol=f"NIFTY{opt_exp:%y%m%d}{int(strike)}{itype.value}",
                    exchange=Exchange.NFO,
                    name="NIFTY",
                    instrument_type=itype,
                    lot_size=lot_size,
                    tick_size=0.05,
                    expiry=opt_exp,
                    strike=strike,
                )
            )
            token += 1
    return SyntheticUniverse(index=index, future=future, options=tuple(options))


class RandomWalkFeed:
    def __init__(
        self,
        universe: SyntheticUniverse,
        *,
        spot: float = 24_000.0,
        annual_vol: float = 0.14,
        implied_vol: float = 0.15,
        step: timedelta = timedelta(seconds=1),
        realtime: bool = True,
        start: datetime | None = None,
        end: datetime | None = None,
        seed: int | None = None,
    ) -> None:
        if not realtime and (start is None or end is None):
            raise ValueError("simulated mode needs start and end")
        self.universe = universe
        self._by_token = {i.token: i for i in universe.all}
        self._spot = spot
        self._vol = annual_vol
        self._iv = implied_vol
        self._step = step
        self._realtime = realtime
        self._now = start
        self._end = end
        self._rng = random.Random(seed)  # noqa: S311 - not cryptographic
        self._tokens: set[int] = set()
        self._volume: dict[int, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self.finished = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def spot(self) -> float:
        return self._spot

    def subscribe(self, tokens: Iterable[int]) -> None:
        for t in tokens:
            if t not in self._by_token:
                raise ValueError(f"unknown synthetic token {t}")
            self._tokens.add(t)

    def unsubscribe(self, tokens: Iterable[int]) -> None:
        self._tokens.difference_update(tokens)

    async def start(self, sink: FeedSink) -> None:
        self._connected = True
        sink.on_feed_status(True, "sim feed started")
        self._task = asyncio.create_task(self._run(sink), name="random-walk-feed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._connected = False

    async def _run(self, sink: FeedSink) -> None:
        try:
            while True:
                if self._realtime:
                    await asyncio.sleep(self._step.total_seconds())
                    ts = now_ist()
                else:
                    if self._now is None or self._end is None or self._now > self._end:
                        break
                    ts = self._now
                    self._now += self._step
                    await asyncio.sleep(0)  # let consumers run
                self._advance()
                ticks = [self._tick(self._by_token[t], ts) for t in sorted(self._tokens)]
                if ticks:
                    sink.on_ticks(ticks)
        finally:
            self._connected = False
            self.finished.set()
            sink.on_feed_status(False, "sim feed stopped")

    def _advance(self) -> None:
        dt = self._step.total_seconds() / TRADING_SECONDS_PER_YEAR
        shock = self._rng.gauss(0.0, 1.0)
        self._spot *= math.exp(-0.5 * self._vol**2 * dt + self._vol * math.sqrt(dt) * shock)

    def price_of(self, inst: Instrument, ts: datetime) -> float:
        if inst.instrument_type is InstrumentType.INDEX:
            return inst.round_price(self._spot)
        if inst.expiry is None:
            raise ValueError(f"{inst.key} has no expiry")
        if inst.instrument_type is InstrumentType.FUT:
            days = max((inst.expiry - ts.date()).days, 0)
            return inst.round_price(self._spot * (1 + 0.065 * days / 365))
        if inst.strike is None:
            raise ValueError(f"{inst.key} has no strike")
        expiry_close = datetime.combine(inst.expiry, datetime.min.time(), ts.tzinfo) + timedelta(
            hours=15, minutes=30
        )
        t_years = max((expiry_close - ts).total_seconds(), 0) / (365 * 24 * 3600)
        price = bs_price(
            self._spot, inst.strike, t_years, self._iv, inst.instrument_type is InstrumentType.CE
        )
        return max(inst.round_price(price), inst.tick_size)

    def _tick(self, inst: Instrument, ts: datetime) -> Tick:
        ltp = self.price_of(inst, ts)
        if inst.instrument_type is InstrumentType.INDEX:
            return Tick(token=inst.token, ts=ts, ltp=ltp)
        # Start from a realistic day volume so liquidity rules see a traded contract.
        vol = self._volume.get(inst.token, 2_000 * inst.lot_size) + (
            self._rng.randint(1, 20) * inst.lot_size
        )
        self._volume[inst.token] = vol
        spread_ticks = 1 if inst.instrument_type is InstrumentType.FUT else 2
        half = inst.tick_size * spread_ticks
        bid = max(inst.round_price(ltp - half), inst.tick_size)
        ask = inst.round_price(ltp + half)
        return Tick(
            token=inst.token,
            ts=ts,
            ltp=ltp,
            volume=vol,
            oi=1_000_000,
            bid=bid,
            ask=ask,
            bid_qty=inst.lot_size * 10,
            ask_qty=inst.lot_size * 10,
        )
