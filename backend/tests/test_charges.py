from __future__ import annotations

import pytest

from app.config import Settings
from app.core.models import Side
from app.engine.charges import FnoCharges
from tests.builders import CE, FUT, fill


def model() -> FnoCharges:
    return FnoCharges.from_settings(Settings(_env_file=None))


def test_option_sell_breakdown() -> None:
    # Sell 75 x Rs 100 premium: turnover 7,500.
    b = model().breakdown(fill(Side.SELL, 75, 100.0, inst=CE))
    assert b.brokerage == 20.0  # flat per order
    assert b.stt == pytest.approx(7500 * 0.15 / 100)  # 11.25, sell side
    assert b.exchange == pytest.approx(7500 * 0.03553 / 100)
    assert b.sebi == pytest.approx(7500 * 10 / 1e7)
    assert b.stamp == 0.0  # buy side only
    assert b.gst == pytest.approx((20 + b.exchange + b.sebi + b.ipft) * 0.18)
    assert b.total == pytest.approx(20 + 11.25 + 2.66475 + 0.0075 + 0.0000075 + b.gst, rel=1e-6)


def test_option_buy_pays_stamp_not_stt() -> None:
    b = model().breakdown(fill(Side.BUY, 75, 100.0, inst=CE))
    assert b.stt == 0.0
    assert b.stamp == pytest.approx(7500 * 0.003 / 100)


def test_futures_brokerage_is_lower_of_pct_or_flat() -> None:
    m = model()
    small = m.breakdown(fill(Side.BUY, 1, 1000.0, inst=FUT))  # turnover 1,000
    assert small.brokerage == pytest.approx(1000 * 0.03 / 100)  # 0.30 < 20
    big = m.breakdown(fill(Side.SELL, 75, 24000.0, inst=FUT))  # turnover 18 lakh
    assert big.brokerage == 20.0
    assert big.stt == pytest.approx(1_800_000 * 0.05 / 100)
    assert big.exchange == pytest.approx(1_800_000 * 0.00183 / 100)


def test_brokerage_charged_once_per_order_across_partial_fills() -> None:
    m = model()
    f1 = fill(Side.SELL, 75, 100.0, inst=CE)
    f2 = type(f1)(**{**{k: getattr(f1, k) for k in f1.__slots__}, "fill_id": "second"})
    assert m.breakdown(f1).brokerage == 20.0
    assert m.breakdown(f2).brokerage == 0.0  # same client_order_id


async def test_engine_net_pnl_includes_charges() -> None:
    from tests.engine_kit import build, feed, liquid  # noqa: PLC0415

    engine, s, _ = await build()
    engine.charges = model()
    await feed(engine, liquid(FUT, 24000.0))
    s.buy(FUT, 75)
    engine._pump()
    pnl = engine.positions.pnl()
    assert pnl.charges > 0
    assert pnl.net == pytest.approx(pnl.gross - pnl.charges)
    assert sum(engine.fill_charges.values()) == pytest.approx(pnl.charges)
    await engine.stop()
