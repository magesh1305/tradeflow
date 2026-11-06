"""Shared domain builders for engine/risk/execution tests."""

from __future__ import annotations

from datetime import date, datetime

from app.core.clock import IST
from app.core.models import (
    Exchange,
    Fill,
    Instrument,
    InstrumentType,
    Product,
    Side,
    Tick,
)

FUT = Instrument(
    token=1001,
    tradingsymbol="NIFTY25NOVFUT",
    exchange=Exchange.NFO,
    name="NIFTY",
    instrument_type=InstrumentType.FUT,
    lot_size=75,
    tick_size=0.1,
    expiry=date(2025, 11, 25),
)
CE = Instrument(
    token=2001,
    tradingsymbol="NIFTY25N1124000CE",
    exchange=Exchange.NFO,
    name="NIFTY",
    instrument_type=InstrumentType.CE,
    lot_size=75,
    tick_size=0.05,
    expiry=date(2025, 11, 11),
    strike=24000.0,
)


def ist(h: int, m: int, s: int = 0, d: date = date(2025, 11, 3)) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST)


def tick(
    inst: Instrument,
    ltp: float,
    ts: datetime,
    *,
    bid: float | None = None,
    ask: float | None = None,
    volume: int = 0,
) -> Tick:
    return Tick(token=inst.token, ts=ts, ltp=ltp, bid=bid, ask=ask, volume=volume)


_fill_seq = iter(range(1, 10**9))


def fill(
    side: Side,
    qty: int,
    price: float,
    *,
    inst: Instrument = FUT,
    strategy: str = "s1",
    product: Product = Product.MIS,
) -> Fill:
    n = next(_fill_seq)
    return Fill(
        fill_id=f"F{n}",
        client_order_id=f"C{n:015d}",
        strategy_id=strategy,
        instrument=inst,
        side=side,
        quantity=qty,
        price=price,
        ts=ist(10, 0),
        product=product,
    )
