"""Live option chains built from ticks, with IV and Greeks (Black-Scholes, via vollib).

- Spot comes from the underlying's index/stock tick, or the nearest future if
  there is no spot tick.
- IV is solved from the mid price when both bid and ask exist, else from the LTP.
- Time to expiry is measured to 15:30 IST on expiry day, in years of 365 days.
- Greeks: delta per 1.0 of the underlying, gamma per 1.0, theta per calendar day,
  vega per 1 volatility point (1%).
A quote with no price, or one priced below intrinsic value, gets no Greeks (None).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

from vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
from vollib.black_scholes.implied_volatility import implied_volatility

from app.core.clock import IST, Clock
from app.core.models import Instrument, InstrumentType, Tick
from app.data.instruments import InstrumentNotFoundError, InstrumentStore

log = logging.getLogger(__name__)

OptionType = Literal["CE", "PE"]
EXPIRY_CLOSE = time(15, 30)
MIN_T = 1 / (365 * 24 * 60)  # one minute, to keep the maths finite on expiry afternoon


@dataclass(frozen=True, slots=True)
class Greeks:
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass(frozen=True, slots=True)
class OptionQuote:
    instrument: Instrument
    ltp: float | None
    bid: float | None
    ask: float | None
    oi: int
    volume: int
    greeks: Greeks | None


@dataclass(frozen=True, slots=True)
class ChainRow:
    strike: float
    call: OptionQuote | None
    put: OptionQuote | None
    is_atm: bool


def year_fraction(now: datetime, expiry: date) -> float:
    close = datetime.combine(expiry, EXPIRY_CLOSE, IST)
    return max((close - now).total_seconds() / (365 * 24 * 3600), MIN_T)


def compute_greeks(
    price: float, spot: float, strike: float, t: float, rate: float, *, call: bool
) -> Greeks | None:
    flag = "c" if call else "p"
    try:
        iv = float(implied_volatility(price, spot, strike, t, rate, flag))
    except Exception:  # below intrinsic / no solution
        return None
    if not 0 < iv < 10:
        return None
    return Greeks(
        iv=iv,
        delta=float(delta(flag, spot, strike, t, rate, iv)),
        gamma=float(gamma(flag, spot, strike, t, rate, iv)),
        theta=float(theta(flag, spot, strike, t, rate, iv)),
        vega=float(vega(flag, spot, strike, t, rate, iv)),
    )


class OptionChainService:
    def __init__(
        self,
        instruments: InstrumentStore,
        quote: Callable[[int], Tick | None],
        clock: Clock,
        rate: float = 0.065,
    ) -> None:
        self._instruments = instruments
        self._quote = quote
        self._clock = clock
        self._rate = rate

    # --- prices -----------------------------------------------------------------

    def spot(self, underlying: str) -> float | None:
        try:
            tick = self._quote(self._instruments.spot(underlying).token)
            if tick is not None:
                return tick.ltp
        except InstrumentNotFoundError:
            pass
        try:
            fut = self._instruments.nearest_future(underlying, self._clock.now().date())
        except InstrumentNotFoundError:
            return None
        tick = self._quote(fut.token)
        return tick.ltp if tick else None

    def _expiry(self, underlying: str, expiry: date | None) -> date:
        return expiry or self._instruments.nearest_expiry(underlying, self._clock.now().date())

    def quote(self, inst: Instrument, spot: float | None) -> OptionQuote:
        tick = self._quote(inst.token)
        greeks = None
        if tick is not None and spot and inst.strike and inst.expiry:
            price = (tick.bid + tick.ask) / 2 if tick.bid and tick.ask else tick.ltp
            if price > 0:
                greeks = compute_greeks(
                    price,
                    spot,
                    inst.strike,
                    year_fraction(self._clock.now(), inst.expiry),
                    self._rate,
                    call=inst.instrument_type is InstrumentType.CE,
                )
        return OptionQuote(
            instrument=inst,
            ltp=tick.ltp if tick else None,
            bid=tick.bid if tick else None,
            ask=tick.ask if tick else None,
            oi=tick.oi if tick else 0,
            volume=tick.volume if tick else 0,
            greeks=greeks,
        )

    # --- chain ------------------------------------------------------------------

    def atm(self, underlying: str, expiry: date | None = None) -> float:
        spot = self.spot(underlying)
        if spot is None:
            raise LookupError(f"no spot price for {underlying} yet")
        return self._instruments.atm_strike(underlying, self._expiry(underlying, expiry), spot)

    def strikes_around_atm(
        self, underlying: str, expiry: date | None = None, width: int = 10
    ) -> list[float]:
        exp = self._expiry(underlying, expiry)
        strikes = self._instruments.strikes(underlying, exp)
        atm = self.atm(underlying, exp)
        i = strikes.index(atm)
        return strikes[max(i - width, 0) : i + width + 1]

    def tokens_around_atm(
        self, underlying: str, expiry: date | None = None, width: int = 10
    ) -> list[int]:
        exp = self._expiry(underlying, expiry)
        tokens = []
        for strike in self.strikes_around_atm(underlying, exp, width):
            for ot in ("CE", "PE"):
                tokens.append(self._instruments.option(underlying, exp, strike, ot).token)
        return tokens

    def chain(
        self, underlying: str, expiry: date | None = None, width: int | None = None
    ) -> list[ChainRow]:
        exp = self._expiry(underlying, expiry)
        spot = self.spot(underlying)
        strikes = self._instruments.strikes(underlying, exp)
        atm = self._instruments.atm_strike(underlying, exp, spot) if spot else None
        if width is not None and atm is not None:
            i = strikes.index(atm)
            strikes = strikes[max(i - width, 0) : i + width + 1]
        rows = []
        for strike in strikes:
            legs: dict[str, OptionQuote | None] = {}
            for ot in ("CE", "PE"):
                try:
                    inst = self._instruments.option(underlying, exp, strike, ot)
                except InstrumentNotFoundError:
                    legs[ot] = None
                    continue
                legs[ot] = self.quote(inst, spot)
            rows.append(ChainRow(strike, legs["CE"], legs["PE"], strike == atm))
        return rows

    # --- selection helpers -----------------------------------------------------------

    def select_by_delta(
        self,
        underlying: str,
        option_type: OptionType,
        target_delta: float,
        expiry: date | None = None,
    ) -> Instrument:
        """Option whose |delta| is closest to |target_delta| (e.g. 0.25)."""
        best: tuple[float, Instrument] | None = None
        for row in self.chain(underlying, expiry):
            q = row.call if option_type == "CE" else row.put
            if q is None or q.greeks is None:
                continue
            gap = abs(abs(q.greeks.delta) - abs(target_delta))
            if best is None or gap < best[0]:
                best = (gap, q.instrument)
        if best is None:
            raise LookupError(f"no {underlying} {option_type} with greeks (subscribe the chain)")
        return best[1]

    def select_by_premium(
        self,
        underlying: str,
        option_type: OptionType,
        target_premium: float,
        expiry: date | None = None,
    ) -> Instrument:
        """Option whose LTP is closest to `target_premium`."""
        best: tuple[float, Instrument] | None = None
        for row in self.chain(underlying, expiry):
            q = row.call if option_type == "CE" else row.put
            if q is None or q.ltp is None:
                continue
            gap = abs(q.ltp - target_premium)
            if best is None or gap < best[0]:
                best = (gap, q.instrument)
        if best is None:
            raise LookupError(f"no {underlying} {option_type} quotes (subscribe the chain)")
        return best[1]
