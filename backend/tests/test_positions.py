from __future__ import annotations

import pytest

from app.core.models import Product, Side
from app.engine.positions import PositionBook
from tests.builders import CE, FUT, fill


def test_open_add_and_average() -> None:
    book = PositionBook()
    book.apply_fill(fill(Side.BUY, 75, 100.0))
    pos = book.apply_fill(fill(Side.BUY, 75, 110.0))
    assert pos.quantity == 150
    assert pos.average_price == pytest.approx(105.0)
    assert pos.realized_pnl == 0
    book.mark(FUT.token, 107.0)
    assert pos.unrealized_pnl == pytest.approx(2.0 * 150)


def test_partial_exit_keeps_average() -> None:
    book = PositionBook()
    book.apply_fill(fill(Side.BUY, 150, 100.0))
    pos = book.apply_fill(fill(Side.SELL, 75, 104.0))
    assert pos.quantity == 75
    assert pos.average_price == pytest.approx(100.0)
    assert pos.realized_pnl == pytest.approx(4.0 * 75)


def test_full_exit_flat() -> None:
    book = PositionBook()
    book.apply_fill(fill(Side.SELL, 75, 200.0))
    pos = book.apply_fill(fill(Side.BUY, 75, 190.0))
    assert pos.quantity == 0
    assert pos.average_price == 0
    assert pos.realized_pnl == pytest.approx(10.0 * 75)
    assert pos.unrealized_pnl == 0
    assert not book.open_positions()


def test_flip_long_to_short() -> None:
    book = PositionBook()
    book.apply_fill(fill(Side.BUY, 75, 100.0))
    pos = book.apply_fill(fill(Side.SELL, 225, 90.0))
    assert pos.quantity == -150
    assert pos.average_price == pytest.approx(90.0)
    assert pos.realized_pnl == pytest.approx(-10.0 * 75)
    book.mark(FUT.token, 80.0)
    assert pos.unrealized_pnl == pytest.approx(10.0 * 150)


def test_flip_short_to_long() -> None:
    book = PositionBook()
    book.apply_fill(fill(Side.SELL, 150, 50.0))
    pos = book.apply_fill(fill(Side.BUY, 225, 40.0))
    assert pos.quantity == 75
    assert pos.average_price == pytest.approx(40.0)
    assert pos.realized_pnl == pytest.approx(10.0 * 150)


def test_keyed_by_strategy_and_product() -> None:
    book = PositionBook()
    book.apply_fill(fill(Side.BUY, 75, 100.0, strategy="a"))
    book.apply_fill(fill(Side.BUY, 75, 100.0, strategy="b"))
    book.apply_fill(fill(Side.BUY, 75, 100.0, strategy="a", product=Product.NRML))
    book.apply_fill(fill(Side.SELL, 75, 10.0, strategy="a", inst=CE))
    assert len(book.open_positions()) == 4
    assert book.net_quantity("a", FUT.token) == 150
    assert book.get("a", FUT.token, Product.NRML) is not None


def test_pnl_summaries_and_charges() -> None:
    book = PositionBook()
    book.apply_fill(fill(Side.BUY, 75, 100.0, strategy="a"), charges=20.0)
    book.apply_fill(fill(Side.SELL, 75, 102.0, strategy="a"), charges=25.0)
    book.apply_fill(fill(Side.SELL, 75, 10.0, strategy="b", inst=CE))
    book.mark(CE.token, 12.0)
    total = book.pnl()
    assert total.realized == pytest.approx(150.0)
    assert total.unrealized == pytest.approx(-150.0)
    assert total.charges == pytest.approx(45.0)
    assert total.gross == pytest.approx(0.0)
    assert total.net == pytest.approx(-45.0)
    by = book.pnl_by_strategy()
    assert by["a"].net == pytest.approx(105.0)
    assert by["b"].gross == pytest.approx(-150.0)
