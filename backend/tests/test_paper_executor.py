from __future__ import annotations

from typing import Any

import pytest

from app.core.clock import ManualClock
from app.core.models import Fill, Order, OrderRequest, OrderStatus, OrderType, Side, Tick
from app.execution.paper import PaperExecutor
from tests.builders import CE, FUT, ist, tick


class Listener:
    def __init__(self) -> None:
        self.ticks: dict[int, Tick] = {}
        self.fills: list[Fill] = []
        self.updates: list[OrderStatus] = []

    def on_order_update(self, order: Order) -> None:
        self.updates.append(order.status)

    def on_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    def last_tick(self, token: int) -> Tick | None:
        return self.ticks.get(token)


def setup(slippage: int = 1) -> tuple[PaperExecutor, Listener]:
    ex = PaperExecutor(ManualClock(ist(10, 0)), slippage_ticks=slippage)
    lst = Listener()
    ex.bind(lst)
    return ex, lst


def order(side: Side, otype: OrderType = OrderType.MARKET, inst: Any = FUT, **kw: Any) -> Order:
    return Order(OrderRequest("s1", inst, side, 75, order_type=otype, **kw))


def feed(ex: PaperExecutor, lst: Listener, t: Tick) -> None:
    lst.ticks[t.token] = t
    ex.on_tick(t)


def test_market_buy_uses_ask_plus_slippage() -> None:
    ex, lst = setup()
    lst.ticks[FUT.token] = tick(FUT, 100.0, ist(10, 0), bid=99.9, ask=100.2)
    o = order(Side.BUY)
    ex.submit(o)
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].price == pytest.approx(100.3)
    assert o.average_price == pytest.approx(100.3)
    assert lst.updates == [OrderStatus.OPEN, OrderStatus.COMPLETE]


def test_market_sell_uses_bid_or_ltp() -> None:
    ex, lst = setup(slippage=2)
    lst.ticks[CE.token] = tick(CE, 50.0, ist(10, 0))  # no depth
    ex.submit(order(Side.SELL, inst=CE))
    assert lst.fills[0].price == pytest.approx(49.9)


def test_market_order_waits_for_first_tick() -> None:
    ex, lst = setup(slippage=0)
    o = order(Side.BUY)
    ex.submit(o)
    assert o.status is OrderStatus.OPEN
    feed(ex, lst, tick(FUT, 101.0, ist(10, 0, 1)))
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].ts == ist(10, 0, 1)


def test_limit_fills_only_when_marketable_with_price_improvement() -> None:
    ex, lst = setup()
    lst.ticks[FUT.token] = tick(FUT, 100.0, ist(10, 0), bid=99.9, ask=100.1)
    o = order(Side.BUY, OrderType.LIMIT, price=99.5)
    ex.submit(o)
    assert o.status is OrderStatus.OPEN
    feed(ex, lst, tick(FUT, 99.6, ist(10, 0, 1), bid=99.5, ask=99.7))
    assert o.status is OrderStatus.OPEN
    feed(ex, lst, tick(FUT, 99.3, ist(10, 0, 2), bid=99.2, ask=99.4))
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].price == pytest.approx(99.4)  # filled at the ask, better than limit


def test_sell_limit() -> None:
    ex, lst = setup()
    o = order(Side.SELL, OrderType.LIMIT, price=101.0)
    ex.submit(o)
    feed(ex, lst, tick(FUT, 100.9, ist(10, 0, 1)))
    assert o.status is OrderStatus.OPEN
    feed(ex, lst, tick(FUT, 101.5, ist(10, 0, 2)))
    assert lst.fills[0].price == pytest.approx(101.5)


def test_slm_buy_stop_triggers_then_fills_at_market() -> None:
    ex, lst = setup(slippage=0)
    lst.ticks[CE.token] = tick(CE, 50.0, ist(10, 0))
    o = order(Side.BUY, OrderType.SL_M, inst=CE, trigger_price=65.0)
    ex.submit(o)
    assert o.status is OrderStatus.TRIGGER_PENDING
    feed(ex, lst, tick(CE, 64.95, ist(10, 0, 1)))
    assert o.status is OrderStatus.TRIGGER_PENDING
    feed(ex, lst, tick(CE, 66.0, ist(10, 0, 2), bid=65.9, ask=66.1))
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].price == pytest.approx(66.1)
    assert OrderStatus.OPEN in lst.updates


def test_sl_limit_triggers_but_waits_for_limit() -> None:
    ex, lst = setup()
    o = order(Side.SELL, OrderType.SL, trigger_price=95.0, price=94.0)
    ex.submit(o)
    feed(ex, lst, tick(FUT, 93.0, ist(10, 0, 1)))  # triggers; 93 < limit 94 so no fill
    assert o.status is OrderStatus.OPEN
    feed(ex, lst, tick(FUT, 94.5, ist(10, 0, 2)))
    assert o.status is OrderStatus.COMPLETE
    assert lst.fills[0].price == pytest.approx(94.5)


def test_cancel_and_modify() -> None:
    ex, lst = setup()
    lst.ticks[FUT.token] = tick(FUT, 100.0, ist(10, 0))
    o = order(Side.BUY, OrderType.LIMIT, price=90.0)
    ex.submit(o)
    ex.modify(o.client_order_id, price=100.04)
    assert o.status is OrderStatus.COMPLETE  # modify made it marketable
    assert o.request.price == pytest.approx(100.0)
    o2 = order(Side.BUY, OrderType.LIMIT, price=90.0)
    ex.submit(o2)
    ex.cancel(o2.client_order_id)
    assert o2.status is OrderStatus.CANCELLED
    feed(ex, lst, tick(FUT, 80.0, ist(10, 0, 5)))
    assert o2.status is OrderStatus.CANCELLED
    ex.cancel(o2.client_order_id)  # idempotent
    assert ex.open_orders == []
