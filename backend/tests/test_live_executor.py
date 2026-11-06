"""LiveExecutor against the real KiteBroker wrapping a fully mocked KiteConnect."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException, NetworkException

from app.brokers.kite import KiteBroker
from app.core.clock import ManualClock
from app.core.models import Fill, Order, OrderRequest, OrderStatus, OrderType, Side, Tick
from app.core.ratelimit import RateLimiter
from app.execution.live import LiveExecutor, map_status
from tests.builders import CE, FUT, ist


class Listener:
    def __init__(self) -> None:
        self.fills: list[Fill] = []
        self.statuses: list[OrderStatus] = []

    def on_order_update(self, order: Order) -> None:
        self.statuses.append(order.status)

    def on_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    def last_tick(self, token: int) -> Tick | None:
        return None


class FastLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__([(1000, 1.0)])

    async def acquire(self) -> None:
        return None


def setup(**kw: Any) -> tuple[LiveExecutor, MagicMock, Listener, ManualClock]:
    kite = MagicMock(spec=KiteConnect)
    kite.place_order.return_value = "OID1"
    kite.orders.return_value = []
    broker = KiteBroker(
        kite, orders_enabled=True, order_limiter=FastLimiter(), data_limiter=FastLimiter()
    )
    clock = ManualClock(ist(10, 0))
    ex = LiveExecutor(broker, clock, **kw)
    lst = Listener()
    ex.bind(lst)
    return ex, kite, lst, clock


def new_order(qty: int = 150, **kw: Any) -> Order:
    return Order(OrderRequest("s1", FUT, Side.BUY, qty, **kw))


def upd(
    order: Order, status: str, filled: int = 0, avg: float = 0.0, **extra: Any
) -> dict[str, Any]:
    return {
        "order_id": order.broker_order_id or "OID1",
        "tag": order.client_order_id,
        "status": status,
        "filled_quantity": filled,
        "pending_quantity": order.request.quantity - filled,
        "average_price": avg,
        "exchange_update_timestamp": "2025-11-03 10:00:05",
        "status_message": None,
        **extra,
    }


async def settle(ex: LiveExecutor) -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    if ex._tasks:
        await asyncio.wait(ex._tasks)


async def test_place_sends_tag_and_market_protection() -> None:
    ex, kite, _, _ = setup()
    o = new_order()
    ex.submit(o)
    await settle(ex)
    kw = kite.place_order.call_args.kwargs
    assert kw["tag"] == o.client_order_id
    assert kw["market_protection"] == -1
    assert o.broker_order_id == "OID1"
    assert o.status is OrderStatus.OPEN


async def test_partial_fills_have_incremental_prices_and_are_idempotent() -> None:
    ex, _, lst, _ = setup()
    o = new_order(qty=225)
    ex.submit(o)
    await settle(ex)
    ex.apply_update(upd(o, "UPDATE", filled=75, avg=100.0))
    ex.apply_update(upd(o, "UPDATE", filled=75, avg=100.0))  # replay: no new fill
    ex.apply_update(upd(o, "UPDATE", filled=150, avg=101.0))
    ex.apply_update(upd(o, "COMPLETE", filled=225, avg=102.0))
    assert [(f.quantity, f.price) for f in lst.fills] == [(75, 100.0), (75, 102.0), (75, 104.0)]
    assert sum(f.quantity * f.price for f in lst.fills) == pytest.approx(225 * 102.0)
    assert [f.fill_id for f in lst.fills] == ["OID1-75", "OID1-150", "OID1-225"]
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].ts == ist(10, 0, 5)
    ex.apply_update(upd(o, "COMPLETE", filled=225, avg=102.0))  # late duplicate
    assert len(lst.fills) == 3


async def test_update_before_place_returns_is_matched_by_tag() -> None:
    ex, _, lst, _ = setup()
    o = new_order(qty=75)
    ex.submit(o)  # placement task not run yet
    ex.apply_update(upd(o, "COMPLETE", filled=75, avg=99.5))
    await settle(ex)
    assert o.status is OrderStatus.COMPLETE
    assert o.broker_order_id == "OID1"
    assert len(lst.fills) == 1


async def test_definite_rejection() -> None:
    ex, kite, _, _ = setup()
    kite.place_order.side_effect = InputException("Insufficient funds")
    o = new_order()
    ex.submit(o)
    await settle(ex)
    assert o.status is OrderStatus.REJECTED
    assert "Insufficient funds" in o.status_message
    assert o.client_order_id not in ex.unknown
    assert kite.place_order.call_count == 1  # never retried


async def test_kite_rejection_update_keeps_message() -> None:
    ex, _, _, _ = setup()
    o = new_order()
    ex.submit(o)
    await settle(ex)
    ex.apply_update(upd(o, "REJECTED", status_message="RMS: margin exceeds"))
    assert o.status is OrderStatus.REJECTED
    assert o.status_message == "RMS: margin exceeds"


async def test_unknown_outcome_resolved_by_reconciliation() -> None:
    ex, kite, lst, _ = setup()
    kite.place_order.side_effect = NetworkException("read timeout")
    o = new_order(qty=75)
    ex.submit(o)
    await settle(ex)
    assert o.status is OrderStatus.PENDING
    assert o.client_order_id in ex.unknown
    assert kite.place_order.call_count == 1  # NOT retried
    # The order did reach Kite: reconciliation finds it by tag.
    kite.orders.return_value = [
        {
            "order_id": "OIDX",
            "tag": o.client_order_id,
            "status": "COMPLETE",
            "filled_quantity": 75,
            "average_price": 101.0,
        }
    ]
    await ex.reconcile()
    assert o.broker_order_id == "OIDX"
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].price == 101.0
    assert not ex.unknown


async def test_unknown_outcome_not_found_becomes_rejected_after_timeout() -> None:
    ex, kite, _, clock = setup(unknown_timeout=60)
    kite.place_order.side_effect = NetworkException("read timeout")
    o = new_order()
    ex.submit(o)
    await settle(ex)
    await ex.reconcile()
    assert o.status is OrderStatus.PENDING  # too early to give up
    clock.advance(timedelta(seconds=61))
    await ex.reconcile()
    assert o.status is OrderStatus.REJECTED
    assert "not found at Kite" in o.status_message


async def test_reconcile_catches_missed_websocket_fill() -> None:
    ex, kite, lst, _ = setup()
    o = new_order(qty=75)
    ex.submit(o)
    await settle(ex)
    kite.orders.return_value = [
        {
            "order_id": "OID1",
            "tag": o.client_order_id,
            "status": "COMPLETE",
            "filled_quantity": 75,
            "average_price": 100.5,
        }
    ]
    await ex.reconcile()
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].price == 100.5


async def test_fill_without_average_price_waits() -> None:
    ex, _, lst, _ = setup()
    o = new_order(qty=75)
    ex.submit(o)
    await settle(ex)
    ex.apply_update(upd(o, "COMPLETE", filled=75, avg=0))
    assert lst.fills == [] and o.status is OrderStatus.OPEN
    ex.apply_update(upd(o, "COMPLETE", filled=75, avg=100.0))
    assert len(lst.fills) == 1 and o.status is OrderStatus.COMPLETE


async def test_cancel_before_placement_completes() -> None:
    ex, kite, _, _ = setup()
    o = new_order()
    ex.submit(o)
    ex.cancel(o.client_order_id)  # no broker id yet
    kite.cancel_order.assert_not_called()
    await settle(ex)
    kite.cancel_order.assert_called_once_with(variety="regular", order_id="OID1")


async def test_cancel_after_partial_fill_keeps_fills() -> None:
    ex, _, lst, _ = setup()
    o = new_order(qty=150)
    ex.submit(o)
    await settle(ex)
    ex.apply_update(upd(o, "UPDATE", filled=75, avg=100.0))
    ex.cancel(o.client_order_id)
    await settle(ex)
    ex.apply_update(upd(o, "CANCELLED", filled=75, avg=100.0, cancelled_quantity=75))
    assert o.status is OrderStatus.CANCELLED
    assert o.filled_quantity == 75
    assert len(lst.fills) == 1


async def test_modify_limit_of_25() -> None:
    ex, kite, _, _ = setup(max_modifications=2)
    o = new_order(order_type=OrderType.LIMIT, price=100.0)
    ex.submit(o)
    await settle(ex)
    for px in (100.1, 100.2, 100.3):
        ex.modify(o.client_order_id, price=px)
        await settle(ex)
    assert kite.modify_order.call_count == 2
    assert o.request.price == pytest.approx(100.2)


async def test_unrelated_orders_ignored() -> None:
    ex, _, lst, _ = setup()
    ex.apply_update(
        {
            "order_id": "MANUAL",
            "tag": None,
            "status": "COMPLETE",
            "filled_quantity": 1,
            "average_price": 5,
        }
    )
    assert lst.fills == []


async def test_adopt_restored_order() -> None:
    ex, kite, lst, _ = setup()
    o = Order(
        OrderRequest("s1", CE, Side.SELL, 75),
        status=OrderStatus.OPEN,
        broker_order_id="OLD1",
        filled_quantity=0,
    )
    ex.adopt(o)
    kite.orders.return_value = [
        {
            "order_id": "OLD1",
            "tag": o.client_order_id,
            "status": "COMPLETE",
            "filled_quantity": 75,
            "average_price": 50.0,
        }
    ]
    await ex.reconcile()
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].strategy_id == "s1"


def test_status_mapping() -> None:
    assert map_status("COMPLETE") is OrderStatus.COMPLETE
    assert map_status("CANCEL") is OrderStatus.CANCELLED
    assert map_status("TRIGGER PENDING") is OrderStatus.TRIGGER_PENDING
    assert map_status("UPDATE") is None
    assert map_status("MODIFY PENDING") is None
    assert map_status("SOMETHING NEW") is None


async def test_reconcile_loop_runs_and_survives_errors() -> None:
    ex, kite, _, _ = setup(reconcile_interval=0.01)
    o = new_order()
    ex.submit(o)
    await settle(ex)
    kite.orders.side_effect = [NetworkException("x"), []] + [[]] * 100
    await ex.start()
    async with asyncio.timeout(5):
        while kite.orders.call_count < 2:  # noqa: ASYNC110 - polling a mock's call count
            await asyncio.sleep(0.01)
    await ex.stop()


# --- auto-slicing (freeze quantity) ---------------------------------------------------------


def sliced_setup(children: list[dict[str, Any]]) -> tuple[LiveExecutor, MagicMock, Listener]:
    ex, kite, lst, _ = setup(freeze_limits={"NIFTY": 1800})
    kite.place_autoslice_order.return_value = {"order_id": "P1", "children": children}
    return ex, kite, lst


def child_upd(child_id: str, status: str, filled: int, avg: float) -> dict[str, Any]:
    # Auto-slice children carry tag=null (verified in the Kite docs).
    return {
        "order_id": child_id,
        "tag": None,
        "status": status,
        "filled_quantity": filled,
        "average_price": avg,
    }


async def test_order_above_freeze_limit_is_autosliced_and_aggregated() -> None:
    ex, kite, lst = sliced_setup([{"order_id": "C1"}, {"order_id": "C2"}])
    o = new_order(qty=3600)
    ex.submit(o)
    await settle(ex)
    kite.place_order.assert_not_called()
    assert kite.place_autoslice_order.call_args.kwargs["quantity"] == 3600
    ex.apply_update(child_upd("C1", "COMPLETE", 1800, 100.0))
    assert o.status is OrderStatus.OPEN and o.filled_quantity == 1800
    ex.apply_update(child_upd("C2", "UPDATE", 900, 102.0))
    ex.apply_update(child_upd("C2", "COMPLETE", 1800, 103.0))
    assert o.status is OrderStatus.COMPLETE
    assert o.filled_quantity == 3600
    assert o.average_price == pytest.approx(101.5)
    assert [(f.quantity, f.price) for f in lst.fills] == [(1800, 100.0), (900, 102.0), (900, 104.0)]
    assert {f.broker_order_id for f in lst.fills} == {"C1", "C2"}


async def test_failed_slice_leaves_partial_fill_cancelled() -> None:
    ex, _, _ = sliced_setup(
        [
            {"order_id": "C1"},
            {
                "error": {
                    "code": 400,
                    "error_type": "MarginException",
                    "message": "Insufficient funds",
                }
            },
        ]
    )
    o = new_order(qty=3600)
    ex.submit(o)
    await settle(ex)
    ex.apply_update(child_upd("C1", "COMPLETE", 1800, 100.0))
    assert o.status is OrderStatus.CANCELLED
    assert o.filled_quantity == 1800
    assert "partially filled 1800/3600" in o.status_message
    assert "Insufficient funds" in o.status_message


async def test_all_slices_failing_is_a_rejection() -> None:
    ex, _, _ = sliced_setup([{"error": {"message": "Insufficient funds"}}])
    o = new_order(qty=3600)
    ex.submit(o)
    await settle(ex)
    assert o.status is OrderStatus.REJECTED


async def test_child_update_before_placement_response_is_replayed() -> None:
    ex, _, lst = sliced_setup([{"order_id": "C1"}, {"order_id": "C2"}])
    o = new_order(qty=3600)
    ex.submit(o)
    ex.apply_update(child_upd("C1", "COMPLETE", 1800, 100.0))  # arrives early: buffered
    assert lst.fills == []
    await settle(ex)
    assert len(lst.fills) == 1 and o.filled_quantity == 1800


async def test_cancel_and_reconcile_cover_every_child() -> None:
    ex, kite, _ = sliced_setup([{"order_id": "C1"}, {"order_id": "C2"}])
    o = new_order(qty=3600)
    ex.submit(o)
    await settle(ex)
    ex.cancel(o.client_order_id)
    await settle(ex)
    assert {c.kwargs["order_id"] for c in kite.cancel_order.call_args_list} == {"C1", "C2"}
    kite.orders.return_value = [
        child_upd("C1", "CANCELLED", 0, 0),
        child_upd("C2", "CANCELLED", 0, 0),
    ]
    await ex.reconcile()
    assert o.status is OrderStatus.CANCELLED


async def test_small_orders_are_not_sliced() -> None:
    ex, kite, _ = sliced_setup([])
    ex.submit(new_order(qty=1800))
    await settle(ex)
    kite.place_order.assert_called_once()
    kite.place_autoslice_order.assert_not_called()
