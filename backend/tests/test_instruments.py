from __future__ import annotations

from datetime import date

import pytest

from app.core.models import InstrumentType
from app.data.instruments import InstrumentNotFoundError, InstrumentStore
from tests.fakes import nifty_rows

TODAY = date(2025, 11, 3)
NEAR = date(2025, 11, 4)
FAR = date(2025, 11, 25)


@pytest.fixture
def store() -> InstrumentStore:
    s = InstrumentStore()
    s.load_kite_rows(nifty_rows(NEAR, FAR), TODAY)
    return s


def test_load_skips_expired_and_other_exchanges(store: InstrumentStore) -> None:
    assert store.find("NFO:NIFTYOLDFUT") is None
    assert store.find("BFO:SENSEXFUT") is None
    assert len(store) == 2 + 2 + 20  # index, stock, 2 futures, 2x5x2 options


def test_index_mapping(store: InstrumentStore) -> None:
    idx = store.spot("NIFTY")
    assert idx.instrument_type is InstrumentType.INDEX
    assert idx.token == 256265
    assert idx.tick_size == 0.05
    assert idx.lot_size == 1
    assert store.spot("RELIANCE").token == 738561


def test_lookup_by_key_and_token(store: InstrumentStore) -> None:
    fut = store.get("NFO:NIFTYNEARFUT")
    assert store.by_token(fut.token) is fut
    with pytest.raises(InstrumentNotFoundError):
        store.get("NFO:NOPE")
    with pytest.raises(InstrumentNotFoundError):
        store.by_token(42)


def test_expiries_and_nearest(store: InstrumentStore) -> None:
    assert store.expiries("NIFTY") == [NEAR, FAR]
    assert store.expiries("NIFTY", "FUT") == [NEAR, FAR]
    assert store.nearest_expiry("NIFTY", TODAY) == NEAR
    assert store.nearest_expiry("NIFTY", NEAR) == NEAR  # expiry day itself counts
    assert store.nearest_expiry("NIFTY", date(2025, 11, 5)) == FAR
    with pytest.raises(InstrumentNotFoundError):
        store.nearest_expiry("NIFTY", date(2026, 1, 1))
    assert store.is_expiry_day("NIFTY", NEAR)
    assert not store.is_expiry_day("NIFTY", TODAY)


def test_futures(store: InstrumentStore) -> None:
    assert [f.tradingsymbol for f in store.futures("NIFTY")] == ["NIFTYNEARFUT", "NIFTYFARFUT"]
    assert store.nearest_future("NIFTY", date(2025, 11, 10)).tradingsymbol == "NIFTYFARFUT"


def test_strikes_and_atm(store: InstrumentStore) -> None:
    assert store.strikes("NIFTY", NEAR) == [23900.0, 23950.0, 24000.0, 24050.0, 24100.0]
    assert store.atm_strike("NIFTY", NEAR, 24012.0) == 24000.0
    assert store.atm_strike("NIFTY", NEAR, 24030.0) == 24050.0
    assert store.atm_strike("NIFTY", NEAR, 24025.0) == 24000.0  # tie -> lower
    assert store.atm_strike("NIFTY", NEAR, 10.0) == 23900.0
    assert store.atm_strike("NIFTY", NEAR, 99999.0) == 24100.0


def test_option_lookup(store: InstrumentStore) -> None:
    pe = store.option("NIFTY", NEAR, 24000, "PE")
    assert pe.instrument_type is InstrumentType.PE
    assert pe.strike == 24000.0
    with pytest.raises(InstrumentNotFoundError):
        store.option("NIFTY", NEAR, 24025, "CE")
    with pytest.raises(InstrumentNotFoundError):
        store.option("NIFTY", date(2030, 1, 1), 24000, "CE")
    # A miss must not create phantom expiries.
    assert store.expiries("NIFTY") == [NEAR, FAR]
    assert len(store.options("NIFTY", NEAR)) == 10


def test_reload_replaces_contents(store: InstrumentStore) -> None:
    store.load_kite_rows(nifty_rows(NEAR, FAR)[:2], TODAY)
    assert len(store) == 2
    assert store.expiries("NIFTY") == []
