from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from kiteconnect import KiteConnect
from kiteconnect.exceptions import NetworkException

from app.brokers.kite import KiteBroker
from app.core.models import OrderRequest, OrderStatus, Side
from app.risk.margin import EstimatedMargin, KiteMargin
from tests.builders import CE, FUT
from tests.engine_kit import PE, ScriptedExecutor, build, feed, liquid
from tests.test_live_executor import FastLimiter


def quote_of(prices: dict[int, float]) -> Any:
    return lambda token: liquid_tick(prices, token)


def liquid_tick(prices: dict[int, float], token: int) -> Any:
    inst = {FUT.token: FUT, CE.token: CE, PE.token: PE}[token]
    return liquid(inst, prices[token]) if token in prices else None


def test_paper_estimates() -> None:
    m = EstimatedMargin(1_000_000, 0.15, quote_of({FUT.token: 24000.0, CE.token: 100.0}))
    assert m.estimate(FUT, Side.BUY, 75, None) == pytest.approx(24000 * 75 * 0.15)
    assert m.estimate(CE, Side.BUY, 75, None) == pytest.approx(100 * 75)  # premium
    assert m.estimate(CE, Side.SELL, 75, None) == pytest.approx(24000 * 75 * 0.15)  # strike


def test_paper_check_counts_existing_usage() -> None:
    m = EstimatedMargin(300_000, 0.15, quote_of({FUT.token: 24000.0}))
    req = OrderRequest("s", FUT, Side.BUY, 75)
    assert m.check_now([req], [], []).ok  # 270k <= 300k
    from app.core.models import Order  # noqa: PLC0415

    pending = [Order(OrderRequest("s", FUT, Side.BUY, 75), status=OrderStatus.OPEN)]
    res = m.check_now([req], [], pending)
    assert not res.ok and "insufficient margin" in res.reason


async def test_engine_rejects_entry_but_never_exits_for_margin() -> None:
    engine, s, _ = await build()
    await feed(engine, liquid(FUT, 24000.0))
    s.buy(FUT, 75)
    engine._pump()
    engine.margin = EstimatedMargin(1.0, 0.15, engine.last_tick)  # broke
    o = s.buy(FUT, 150)  # different size, so the duplicate-order guard doesn't fire first
    assert o.status is OrderStatus.REJECTED and "insufficient margin" in o.status_message
    exits = s.exit_position(FUT)
    engine._pump()
    assert exits and exits[0].status is OrderStatus.COMPLETE
    await engine.stop()


def kite_margin(**returns: Any) -> tuple[KiteMargin, MagicMock]:
    kite = MagicMock(spec=KiteConnect)
    kite.margins.return_value = {"net": returns.get("net", 500_000.0)}
    kite.order_margins.return_value = [{"total": returns.get("single", 150_000.0)}]
    kite.basket_order_margins.return_value = {
        "initial": {"total": 400_000.0},
        "final": {"total": returns.get("basket", 120_000.0)},
    }
    broker = KiteBroker(
        kite, orders_enabled=True, order_limiter=FastLimiter(), data_limiter=FastLimiter()
    )
    return KiteMargin(broker), kite


async def test_kite_single_order_margin() -> None:
    m, kite = kite_margin(single=150_000.0, net=100_000.0)
    res = await m.check([OrderRequest("s", FUT, Side.BUY, 75)])
    assert not res.ok and res.required == 150_000.0 and res.available == 100_000.0
    (params,) = kite.order_margins.call_args.args[0]
    assert params["tradingsymbol"] == FUT.tradingsymbol
    assert params["variety"] == "regular"
    assert "tag" not in params and "market_protection" not in params
    kite.margins.assert_called_once_with("equity")


async def test_kite_basket_uses_hedge_benefit() -> None:
    m, kite = kite_margin(basket=120_000.0, net=200_000.0)
    res = await m.check(
        [OrderRequest("s", CE, Side.SELL, 75), OrderRequest("s", PE, Side.SELL, 75)]
    )
    assert res.ok and res.required == 120_000.0  # final.total, not initial
    assert kite.basket_order_margins.call_args.kwargs == {"consider_positions": True}


async def test_kite_margin_fails_closed() -> None:
    m, kite = kite_margin()
    kite.margins.side_effect = NetworkException("timeout")
    res = await m.check([OrderRequest("s", FUT, Side.BUY, 75)])
    assert not res.ok and "margin check failed" in res.reason


async def test_engine_async_margin_path() -> None:
    ex = ScriptedExecutor()
    m, _ = kite_margin(single=100.0, net=1_000.0)
    engine, s, _ = await build(executor=ex, margin=m)
    await feed(engine, liquid(FUT, 24000.0))
    o = s.buy(FUT, 75)
    assert o.status is OrderStatus.PENDING and ex.submitted == []  # waiting for Kite
    await asyncio.sleep(0.05)
    assert ex.submitted == [o] and o.status is OrderStatus.OPEN
    # Cancelled while the margin check is in flight: never sent.
    o2 = s.sell(FUT, 75)
    engine.cancel_order(o2.client_order_id)
    await asyncio.sleep(0.05)
    assert o2.status is OrderStatus.CANCELLED and o2 not in ex.submitted
    await engine.stop()
