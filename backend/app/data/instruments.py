"""Instrument master: NSE (cash + indices) and NFO (futures + options).

Loaded from Kite's instrument CSV (via KiteBroker.instruments) or from
synthetic instruments (RandomWalkFeed). Expired contracts are dropped on load.
"""

from __future__ import annotations

import bisect
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any, Literal

from app.core.models import Exchange, Instrument, InstrumentType

log = logging.getLogger(__name__)

# Underlying name (as used in NFO contracts) -> spot instrument key.
DEFAULT_SPOT_KEYS: dict[str, str] = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
}

ContractKind = Literal["FUT", "OPT"]
OptionType = Literal["CE", "PE"]
SUPPORTED_EXCHANGES = frozenset({"NSE", "NFO"})


class InstrumentNotFoundError(KeyError):
    pass


def instrument_from_kite(row: Mapping[str, Any]) -> Instrument | None:
    """Convert one row of Kite's instrument dump. Returns None for unsupported rows."""
    exchange = str(row["exchange"])
    if exchange not in SUPPORTED_EXCHANGES:
        return None
    segment = str(row.get("segment", ""))
    raw_type = str(row["instrument_type"])
    if segment == "INDICES":
        itype = InstrumentType.INDEX
    elif raw_type in InstrumentType.__members__:
        itype = InstrumentType(raw_type)
    else:
        return None
    expiry = row.get("expiry")
    strike = float(row.get("strike") or 0.0)
    tick = float(row.get("tick_size") or 0.0)
    lot = int(row.get("lot_size") or 0)
    return Instrument(
        token=int(row["instrument_token"]),
        tradingsymbol=str(row["tradingsymbol"]),
        exchange=Exchange(exchange),
        # Indices have an empty `name`; fall back to the symbol.
        name=str(row.get("name") or row["tradingsymbol"]),
        instrument_type=itype,
        lot_size=lot if lot > 0 else 1,  # indices report 0
        tick_size=tick if tick > 0 else 0.05,  # indices report 0
        expiry=expiry if isinstance(expiry, date) else None,
        strike=strike if itype in (InstrumentType.CE, InstrumentType.PE) else None,
    )


class InstrumentStore:
    def __init__(self, spot_keys: Mapping[str, str] | None = None) -> None:
        self._spot_keys = dict(spot_keys or DEFAULT_SPOT_KEYS)
        self._clear()

    def _clear(self) -> None:
        self._by_token: dict[int, Instrument] = {}
        self._by_key: dict[str, Instrument] = {}
        self._futures: dict[str, list[Instrument]] = defaultdict(list)
        # underlying -> expiry -> (strike, type) -> instrument
        self._options: dict[str, dict[date, dict[tuple[float, str], Instrument]]] = defaultdict(
            lambda: defaultdict(dict)
        )

    def __len__(self) -> int:
        return len(self._by_token)

    # --- loading ----------------------------------------------------------------

    def load(self, instruments: Iterable[Instrument], today: date) -> None:
        """Replace the store's contents. Contracts that expired before `today` are skipped."""
        self._clear()
        for inst in instruments:
            if inst.expiry is not None and inst.expiry < today:
                continue
            self._add(inst)
        for futs in self._futures.values():
            futs.sort(key=lambda i: i.expiry or date.max)
        log.info("instruments loaded", extra={"count": len(self._by_token)})

    def load_kite_rows(self, rows: Iterable[Mapping[str, Any]], today: date) -> None:
        converted = (instrument_from_kite(r) for r in rows)
        self.load((i for i in converted if i is not None), today)

    def _add(self, inst: Instrument) -> None:
        self._by_token[inst.token] = inst
        self._by_key[inst.key] = inst
        if inst.instrument_type is InstrumentType.FUT:
            self._futures[inst.name].append(inst)
        elif inst.is_option and inst.expiry is not None and inst.strike is not None:
            self._options[inst.name][inst.expiry][(inst.strike, inst.instrument_type.value)] = inst

    # --- lookups ----------------------------------------------------------------

    def get(self, key: str) -> Instrument:
        """Lookup by Kite key, e.g. 'NFO:NIFTY25NOVFUT' or 'NSE:NIFTY 50'."""
        try:
            return self._by_key[key]
        except KeyError:
            raise InstrumentNotFoundError(key) from None

    def by_token(self, token: int) -> Instrument:
        try:
            return self._by_token[token]
        except KeyError:
            raise InstrumentNotFoundError(token) from None

    def find(self, key: str) -> Instrument | None:
        return self._by_key.get(key)

    def all(self) -> list[Instrument]:
        return list(self._by_token.values())

    def spot(self, underlying: str) -> Instrument:
        """Spot/index instrument: NIFTY -> NSE:NIFTY 50, RELIANCE -> NSE:RELIANCE."""
        return self.get(self._spot_keys.get(underlying, f"NSE:{underlying}"))

    def underlyings(self) -> list[str]:
        return sorted(set(self._futures) | set(self._options))

    # --- expiries ---------------------------------------------------------------

    def expiries(self, underlying: str, kind: ContractKind = "OPT") -> list[date]:
        if kind == "FUT":
            return sorted({f.expiry for f in self._futures.get(underlying, []) if f.expiry})
        return sorted(self._options.get(underlying, {}))

    def nearest_expiry(self, underlying: str, today: date, kind: ContractKind = "OPT") -> date:
        for exp in self.expiries(underlying, kind):
            if exp >= today:
                return exp
        raise InstrumentNotFoundError(f"no {kind} expiry for {underlying} on/after {today}")

    def is_expiry_day(self, underlying: str, today: date) -> bool:
        return today in self.expiries(underlying, "OPT") or today in self.expiries(
            underlying, "FUT"
        )

    # --- futures ----------------------------------------------------------------

    def futures(self, underlying: str) -> list[Instrument]:
        return list(self._futures.get(underlying, []))

    def nearest_future(self, underlying: str, today: date) -> Instrument:
        for fut in self._futures.get(underlying, []):
            if fut.expiry and fut.expiry >= today:
                return fut
        raise InstrumentNotFoundError(f"no future for {underlying} on/after {today}")

    # --- options ----------------------------------------------------------------

    def strikes(self, underlying: str, expiry: date) -> list[float]:
        chain = self._options.get(underlying, {}).get(expiry, {})
        return sorted({strike for strike, _ in chain})

    def atm_strike(self, underlying: str, expiry: date, spot_price: float) -> float:
        strikes = self.strikes(underlying, expiry)
        if not strikes:
            raise InstrumentNotFoundError(f"no strikes for {underlying} {expiry}")
        i = bisect.bisect_left(strikes, spot_price)
        candidates = strikes[max(i - 1, 0) : i + 1]
        # Nearest strike; ties go to the lower strike.
        return min(candidates, key=lambda s: (abs(s - spot_price), s))

    def option(
        self, underlying: str, expiry: date, strike: float, option_type: OptionType
    ) -> Instrument:
        # .get() chain: indexing the defaultdicts would insert empty entries on a miss.
        inst = self._options.get(underlying, {}).get(expiry, {}).get((float(strike), option_type))
        if inst is None:
            raise InstrumentNotFoundError(f"{underlying} {expiry} {strike} {option_type}")
        return inst

    def next_contract(self, inst: Instrument) -> Instrument:
        """Same contract in the next expiry (roll target): next future, or same strike/type."""
        if inst.expiry is None:
            raise InstrumentNotFoundError(f"{inst.key} does not expire")
        if inst.instrument_type is InstrumentType.FUT:
            for fut in self._futures.get(inst.name, []):
                if fut.expiry and fut.expiry > inst.expiry:
                    return fut
            raise InstrumentNotFoundError(f"no future after {inst.key}")
        if inst.strike is None:
            raise InstrumentNotFoundError(f"{inst.key} has no strike")
        for exp in self.expiries(inst.name, "OPT"):
            if exp > inst.expiry:
                return self.option(inst.name, exp, inst.strike, inst.instrument_type.value)  # type: ignore[arg-type]
        raise InstrumentNotFoundError(f"no expiry after {inst.key}")

    def options(self, underlying: str, expiry: date) -> list[Instrument]:
        chain = self._options.get(underlying, {}).get(expiry, {})
        return sorted(chain.values(), key=lambda i: (i.strike or 0.0, i.instrument_type))


def weekdays_until(today: date, expiry: date) -> int:
    """Trading days from `today` to `expiry` counting weekdays only (0 on expiry day).
    Exchange holidays are not modelled."""
    if expiry <= today:
        return 0
    days = 0
    d = today
    while d < expiry:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days
