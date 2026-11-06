from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.clock import IST, ManualClock
from app.core.models import Tick
from app.data.instruments import InstrumentStore
from app.data.option_chain import OptionChainService, compute_greeks, year_fraction
from app.feeds.random_walk import bs_price, synthetic_nifty

TODAY = date(2025, 11, 3)
NOW = datetime(2025, 11, 3, 10, 0, tzinfo=IST)
EXPIRY = date(2025, 11, 4)


def service(with_spot: bool = True) -> tuple[OptionChainService, dict[int, Tick]]:
    u = synthetic_nifty(TODAY)
    store = InstrumentStore()
    store.load(u.all, TODAY)
    ticks: dict[int, Tick] = {}
    spot = 24010.0
    if with_spot:
        ticks[u.index.token] = Tick(u.index.token, NOW, spot)
    t = year_fraction(NOW, EXPIRY)
    for o in u.options:
        assert o.strike is not None
        px = round(bs_price(spot, o.strike, t, 0.15, o.instrument_type == "CE"), 2)
        if px >= 0.05:
            ticks[o.token] = Tick(o.token, NOW, px, volume=1000, oi=5000)
    svc = OptionChainService(store, ticks.get, ManualClock(NOW), rate=0.0)
    return svc, ticks


def test_chain_rows_with_iv_and_greeks() -> None:
    svc, _ = service()
    rows = svc.chain("NIFTY", EXPIRY, width=5)
    assert len(rows) == 11
    (atm,) = [r for r in rows if r.is_atm]
    assert atm.strike == 24000.0
    assert atm.call is not None and atm.call.greeks is not None
    g = atm.call.greeks
    assert g.iv == pytest.approx(0.15, abs=0.01)
    assert 0.45 < g.delta < 0.65
    assert g.gamma > 0 and g.theta < 0 and g.vega > 0
    assert atm.put is not None and atm.put.greeks is not None
    assert -0.55 < atm.put.greeks.delta < -0.35
    assert atm.call.oi == 5000


def test_selection_helpers() -> None:
    svc, ticks = service()
    assert svc.atm("NIFTY") == 24000.0
    ce = svc.select_by_delta("NIFTY", "CE", 0.25)
    assert ce.strike is not None and ce.strike > 24000
    pe = svc.select_by_delta("NIFTY", "PE", 0.25)
    assert pe.strike is not None and pe.strike < 24000
    target = ticks[ce.token].ltp
    assert svc.select_by_premium("NIFTY", "CE", target) == ce
    assert len(svc.tokens_around_atm("NIFTY", width=2)) == 10


def test_missing_quotes_and_spot() -> None:
    svc, _ = service(with_spot=False)
    with pytest.raises(LookupError):
        svc.atm("NIFTY")
    rows = svc.chain("NIFTY", EXPIRY)
    assert all(r.call is None or r.call.greeks is None for r in rows)  # no spot, no greeks


def test_below_intrinsic_has_no_greeks() -> None:
    assert compute_greeks(1.0, 24000, 23000, 5 / 365, 0.065, call=True) is None
    assert compute_greeks(1200.0, 24000, 23000, 5 / 365, 0.065, call=True) is not None
