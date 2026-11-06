from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

import pytest

from app.core.clock import IST, ManualClock, next_occurrence, to_ist
from app.core.logging import JsonFormatter
from app.core.models import (
    Exchange,
    Instrument,
    InstrumentType,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    new_client_order_id,
)

FUT = Instrument(
    token=1,
    tradingsymbol="NIFTY25NOVFUT",
    exchange=Exchange.NFO,
    name="NIFTY",
    instrument_type=InstrumentType.FUT,
    lot_size=75,
    tick_size=0.05,
    expiry=date(2025, 11, 25),
)


def test_client_order_id_is_valid_kite_tag() -> None:
    ids = {new_client_order_id() for _ in range(1000)}
    assert len(ids) == 1000
    for cid in ids:
        assert len(cid) == 16
        assert cid.isalnum()
        assert cid.isascii()


def test_side_helpers() -> None:
    assert Side.BUY.sign == 1
    assert Side.SELL.sign == -1
    assert Side.BUY.opposite is Side.SELL
    assert OrderType.SL_M.value == "SL-M"


def test_instrument_key_and_rounding() -> None:
    assert FUT.key == "NFO:NIFTY25NOVFUT"
    assert FUT.round_price(100.03) == 100.05
    assert FUT.round_price(100.02) == 100.0
    assert not FUT.is_option


def test_order_request_validation() -> None:
    req = OrderRequest("s1", FUT, Side.SELL, 75)
    assert req.signed_quantity == -75
    with pytest.raises(ValueError, match="positive"):
        OrderRequest("s1", FUT, Side.BUY, 0)
    with pytest.raises(ValueError, match="price"):
        OrderRequest("s1", FUT, Side.BUY, 75, order_type=OrderType.LIMIT)
    with pytest.raises(ValueError, match="trigger_price"):
        OrderRequest("s1", FUT, Side.BUY, 75, order_type=OrderType.SL_M)
    with pytest.raises(ValueError, match="client_order_id"):
        OrderRequest("s1", FUT, Side.BUY, 75, client_order_id="bad-id")


def test_order_pending_quantity() -> None:
    order = Order(OrderRequest("s1", FUT, Side.BUY, 150))
    order.filled_quantity = 75
    order.status = OrderStatus.OPEN
    assert order.pending_quantity == 75
    order.status = OrderStatus.CANCELLED
    assert order.pending_quantity == 0
    assert OrderStatus.CANCELLED.is_terminal
    assert not OrderStatus.TRIGGER_PENDING.is_terminal


def test_clock_helpers() -> None:
    naive = datetime(2025, 11, 3, 9, 15)  # noqa: DTZ001 - naive on purpose
    assert to_ist(naive).tzinfo is IST
    utc = datetime(2025, 11, 3, 3, 45, tzinfo=IST).astimezone(IST)
    assert to_ist(utc).hour == 3

    before = datetime(2025, 11, 3, 5, 0, tzinfo=IST)
    after = datetime(2025, 11, 3, 7, 0, tzinfo=IST)
    assert next_occurrence(before, time(6, 0)) == datetime(2025, 11, 3, 6, 0, tzinfo=IST)
    assert next_occurrence(after, time(6, 0)) == datetime(2025, 11, 4, 6, 0, tzinfo=IST)

    clock = ManualClock(before)
    clock.advance(timedelta(minutes=5))
    assert clock.now() == before + timedelta(minutes=5)


def test_json_formatter_includes_extra() -> None:
    record = logging.makeLogRecord(
        {"name": "t", "levelname": "INFO", "msg": "hello %s", "args": ("x",), "strategy": "ema1"}
    )
    out = json.loads(JsonFormatter().format(record))
    assert out["msg"] == "hello x"
    assert out["strategy"] == "ema1"
    assert out["ts"].endswith("+05:30")
