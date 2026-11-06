from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException, TokenException

from app.brokers.kite import BrokerError, KiteBroker, OrdersDisabledError, SessionExpiredError
from app.core.clock import IST
from app.core.models import (
    Exchange,
    Instrument,
    InstrumentType,
    OrderRequest,
    OrderType,
    Product,
    Side,
)
from app.core.ratelimit import RateLimiter

OPT = Instrument(
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


class CountingLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__([(1000, 1.0)])
        self.calls = 0

    async def acquire(self) -> None:
        self.calls += 1


def make(
    orders_enabled: bool = True, mp: float = -1
) -> tuple[KiteBroker, MagicMock, CountingLimiter]:
    kite = MagicMock(spec=KiteConnect)
    kite.place_order.return_value = "250101000000001"
    kite.modify_order.return_value = "250101000000001"
    kite.cancel_order.return_value = "250101000000001"
    limiter = CountingLimiter()
    broker = KiteBroker(
        kite, orders_enabled=orders_enabled, market_protection=mp, order_limiter=limiter
    )
    return broker, kite, limiter


def req(order_type: OrderType, **kw: Any) -> OrderRequest:
    return OrderRequest("s1", OPT, Side.BUY, 75, order_type=order_type, **kw)


@pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.SL_M])
async def test_market_and_slm_carry_market_protection(order_type: OrderType) -> None:
    broker, kite, limiter = make(mp=-1)
    r = req(order_type, trigger_price=99.0) if order_type is OrderType.SL_M else req(order_type)
    oid = await broker.place_order(r)
    assert oid == "250101000000001"
    kw = kite.place_order.call_args.kwargs
    assert kw["market_protection"] == -1
    assert kw["tag"] == r.client_order_id
    assert kw["variety"] == "regular"
    assert kw["exchange"] == "NFO"
    assert kw["transaction_type"] == "BUY"
    assert kw["product"] == "MIS"
    assert kw["order_type"] == order_type.value
    assert kw["validity"] == "DAY"
    assert "price" not in kw
    assert limiter.calls == 1


@pytest.mark.parametrize("order_type", [OrderType.LIMIT, OrderType.SL])
async def test_limit_and_sl_have_no_market_protection(order_type: OrderType) -> None:
    broker, kite, _ = make()
    await broker.place_order(req(order_type, price=100.03, trigger_price=99.98))
    kw = kite.place_order.call_args.kwargs
    assert "market_protection" not in kw
    assert kw["price"] == 100.05  # rounded to tick
    if order_type is OrderType.SL:
        assert kw["trigger_price"] == 100.0
    else:
        assert "trigger_price" not in kw


async def test_configured_market_protection_value_is_used() -> None:
    broker, kite, _ = make(mp=2.5)
    await broker.place_order(req(OrderType.MARKET))
    assert kite.place_order.call_args.kwargs["market_protection"] == 2.5


async def test_params_accepted_by_real_sdk_signature() -> None:
    """Bind our params against the real KiteConnect.place_order signature."""
    import inspect  # noqa: PLC0415

    broker, _, _ = make()
    params = broker.order_params(req(OrderType.SL_M, trigger_price=99.0, product=Product.NRML))
    inspect.signature(KiteConnect.place_order).bind(None, **params)
    inspect.signature(KiteConnect.place_autoslice_order).bind(None, **params)


async def test_orders_disabled_in_paper_mode() -> None:
    broker, kite, limiter = make(orders_enabled=False)
    with pytest.raises(OrdersDisabledError):
        await broker.place_order(req(OrderType.MARKET))
    with pytest.raises(OrdersDisabledError):
        await broker.place_autoslice_order(req(OrderType.MARKET))
    with pytest.raises(OrdersDisabledError):
        await broker.modify_order("1", price=10)
    with pytest.raises(OrdersDisabledError):
        await broker.cancel_order("1")
    kite.place_order.assert_not_called()
    kite.place_autoslice_order.assert_not_called()
    kite.modify_order.assert_not_called()
    kite.cancel_order.assert_not_called()
    assert limiter.calls == 0


async def test_data_calls_work_when_orders_disabled() -> None:
    broker, kite, _ = make(orders_enabled=False)
    kite.positions.return_value = {"net": [], "day": []}
    assert await broker.positions() == {"net": [], "day": []}


async def test_autoslice_returns_parent_and_children() -> None:
    broker, kite, _ = make()
    kite.place_autoslice_order.return_value = {
        "order_id": "P1",
        "children": [{"order_id": "C1"}, {"order_id": "C2"}],
    }
    resp = await broker.place_autoslice_order(req(OrderType.MARKET))
    assert resp["children"][1]["order_id"] == "C2"
    assert kite.place_autoslice_order.call_args.kwargs["market_protection"] == -1


async def test_modify_and_cancel_go_through_order_limiter() -> None:
    broker, kite, limiter = make()
    await broker.modify_order("OID", price=101.0, order_type=OrderType.SL_M)
    kw = kite.modify_order.call_args.kwargs
    assert kw == {
        "variety": "regular",
        "order_id": "OID",
        "price": 101.0,
        "order_type": "SL-M",
        "market_protection": -1,
    }
    await broker.cancel_order("OID")
    kite.cancel_order.assert_called_once_with(variety="regular", order_id="OID")
    assert limiter.calls == 2


async def test_token_exception_maps_to_session_expired_and_hook() -> None:
    kite = MagicMock(spec=KiteConnect)
    kite.orders.side_effect = TokenException("Incorrect `api_key` or `access_token`.")
    expired = MagicMock()
    broker = KiteBroker(kite, orders_enabled=True, on_session_expired=expired)
    with pytest.raises(SessionExpiredError):
        await broker.orders()
    expired.assert_called_once()


async def test_order_errors_are_not_retried() -> None:
    broker, kite, _ = make()
    kite.place_order.side_effect = InputException("Insufficient funds")
    with pytest.raises(BrokerError, match="Insufficient funds"):
        await broker.place_order(req(OrderType.MARKET))
    assert kite.place_order.call_count == 1


async def test_historical_passes_naive_ist() -> None:
    broker, kite, _ = make()
    kite.historical_data.return_value = []
    start = datetime(2025, 11, 3, 3, 45, tzinfo=IST).astimezone(IST)
    await broker.historical_data(256265, start, start, "minute", oi=True)
    args = kite.historical_data.call_args
    assert args.args[1].tzinfo is None
    assert args.args[1].hour == 3
    assert args.kwargs == {"continuous": False, "oi": True}
