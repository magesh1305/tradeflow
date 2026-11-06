from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.core.clock import ManualClock
from app.core.models import Order, OrderRequest, OrderStatus, Product, Side
from app.engine.positions import PositionBook
from app.risk.manager import RiskLimits, RiskManager
from tests.builders import CE, FUT, fill, ist


def setup(**limits: Any) -> tuple[RiskManager, PositionBook, ManualClock]:
    clock = ManualClock(ist(10, 0))
    book = PositionBook()
    base: dict[str, Any] = {"duplicate_window_sec": 2.0}
    base.update(limits)
    return RiskManager(RiskLimits(**base), book, clock), book, clock


def req(side: Side = Side.BUY, qty: int = 75, inst: Any = FUT, **kw: Any) -> OrderRequest:
    return OrderRequest(kw.pop("strategy", "s1"), inst, side, qty, **kw)


def test_allows_normal_entry() -> None:
    risk, _, _ = setup()
    d = risk.check(req())
    assert d.allowed and not d.is_exit


def test_lot_size_multiple() -> None:
    risk, _, _ = setup()
    d = risk.check(req(qty=80))
    assert not d.allowed and "lot size" in d.reason


def test_max_lots_per_order() -> None:
    risk, _, _ = setup(max_lots_per_order=2)
    assert risk.check(req(qty=150)).allowed
    d = risk.check(req(qty=225, strategy="s2"))
    assert not d.allowed and "max 2" in d.reason


@pytest.mark.parametrize(
    ("h", "m", "ok"), [(9, 14, False), (9, 15, True), (15, 9, True), (15, 10, False)]
)
def test_trading_window(h: int, m: int, ok: bool) -> None:
    risk, _, clock = setup()
    clock.set(ist(h, m))
    assert risk.check(req()).allowed is ok


def test_max_open_positions_counts_pending_entries() -> None:
    risk, book, _ = setup(max_open_positions=2)
    book.apply_fill(fill(Side.BUY, 75, 100.0, strategy="a"))
    pending = [Order(req(inst=CE, strategy="b"), status=OrderStatus.OPEN)]
    d = risk.check(req(strategy="c"), pending)
    assert not d.allowed and "open positions" in d.reason
    # Adding to an existing position does not open a new one.
    assert risk.check(req(strategy="a"), pending).allowed


def test_orders_per_minute() -> None:
    risk, _, clock = setup(max_orders_per_minute=3, duplicate_window_sec=0)
    for i in range(3):
        assert risk.check(req(strategy=f"s{i}")).allowed
    assert "per minute" in risk.check(req(strategy="x")).reason
    clock.advance(timedelta(seconds=61))
    assert risk.check(req(strategy="x")).allowed


def test_duplicate_guard() -> None:
    risk, _, clock = setup()
    assert risk.check(req()).allowed
    d = risk.check(req())
    assert not d.allowed and "duplicate" in d.reason
    assert risk.check(req(side=Side.SELL)).allowed  # different side is fine
    clock.advance(timedelta(seconds=3))
    assert risk.check(req()).allowed


def test_exits_always_allowed_even_with_kill_switch_and_outside_window() -> None:
    risk, book, clock = setup(max_lots_per_order=1)
    book.apply_fill(fill(Side.BUY, 150, 100.0))
    risk.kill_switch.trip("test", clock.now())
    clock.set(ist(15, 25))
    d = risk.check(req(side=Side.SELL, qty=150))  # 2 lots > max 1, still allowed
    assert d.allowed and d.is_exit
    assert not risk.check(req(side=Side.BUY)).allowed  # entries blocked


def test_exit_must_match_product() -> None:
    risk, book, clock = setup()
    book.apply_fill(fill(Side.BUY, 75, 100.0, product=Product.NRML))
    risk.kill_switch.trip("t", clock.now())
    assert not risk.check(req(side=Side.SELL, product=Product.MIS)).allowed
    assert risk.check(req(side=Side.SELL, product=Product.NRML)).is_exit


def test_flip_is_not_an_exit() -> None:
    risk, book, clock = setup()
    book.apply_fill(fill(Side.BUY, 75, 100.0))
    risk.kill_switch.trip("t", clock.now())
    d = risk.check(req(side=Side.SELL, qty=150))
    assert not d.allowed and not d.is_exit


def test_pending_exits_are_counted() -> None:
    risk, book, clock = setup()
    book.apply_fill(fill(Side.BUY, 75, 100.0))
    pending = [Order(req(side=Side.SELL), status=OrderStatus.OPEN)]
    risk.kill_switch.trip("t", clock.now())
    # A second full exit would flip the position, so it is not an exit.
    assert not risk.check(req(side=Side.SELL), pending).allowed


def test_daily_loss_trips_kill_switch_once() -> None:
    risk, _, _ = setup(daily_loss_limit=5000)
    assert risk.check_daily_loss(-4999.0) is False
    assert risk.check_daily_loss(-5000.0) is True
    assert risk.kill_switch.tripped
    assert "daily loss" in risk.kill_switch.reason
    assert risk.check_daily_loss(-9000.0) is False  # already tripped
    risk.kill_switch.reset()
    assert not risk.kill_switch.tripped
