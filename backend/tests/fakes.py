"""Test doubles and builders (no network)."""

from __future__ import annotations

from datetime import date
from typing import Any


def kite_row(
    token: int,
    symbol: str,
    *,
    exchange: str = "NFO",
    segment: str = "NFO-OPT",
    itype: str = "CE",
    name: str = "NIFTY",
    expiry: date | str = "",
    strike: float = 0.0,
    lot: int = 75,
    tick: float = 0.05,
) -> dict[str, Any]:
    """A row shaped like kiteconnect's parsed instrument dump."""
    return {
        "instrument_token": token,
        "exchange_token": str(token // 256),
        "tradingsymbol": symbol,
        "name": name,
        "last_price": 0.0,
        "expiry": expiry,
        "strike": strike,
        "tick_size": tick,
        "lot_size": lot,
        "instrument_type": itype,
        "segment": segment,
        "exchange": exchange,
    }


def nifty_rows(near: date, far: date) -> list[dict[str, Any]]:
    rows = [
        kite_row(
            256265,
            "NIFTY 50",
            exchange="NSE",
            segment="INDICES",
            itype="EQ",
            name="",
            lot=0,
            tick=0,
        ),
        kite_row(
            738561, "RELIANCE", exchange="NSE", segment="NSE", itype="EQ", name="RELIANCE", lot=1
        ),
        kite_row(1001, "NIFTYNEARFUT", segment="NFO-FUT", itype="FUT", expiry=near, tick=0.1),
        kite_row(1002, "NIFTYFARFUT", segment="NFO-FUT", itype="FUT", expiry=far, tick=0.1),
        kite_row(1, "NIFTYOLDFUT", segment="NFO-FUT", itype="FUT", expiry=date(2020, 1, 30)),
        kite_row(5, "SENSEXFUT", exchange="BFO", segment="BFO-FUT", itype="FUT", expiry=near),
    ]
    token = 2000
    for exp in (near, far):
        for strike in (23900.0, 23950.0, 24000.0, 24050.0, 24100.0):
            for t in ("CE", "PE"):
                rows.append(
                    kite_row(
                        token,
                        f"NIFTY{exp:%y%m%d}{int(strike)}{t}",
                        itype=t,
                        expiry=exp,
                        strike=strike,
                    )
                )
                token += 1
    return rows
