"""Liquidity-aware order handling for options.

- `prepare`: a MARKET request for an option becomes a LIMIT order priced from the
  quote. Entries use the far side plus `buffer_ticks` ticks; exits use the far side
  plus `exit_buffer_pct` percent (exits must get done). An exit with no quote stays
  MARKET (Kite applies market protection). Protective stops are never converted.
- `Chaser`: a converted order still open after `chase_after_sec` is re-priced
  toward the market, one more step each time, at most `chase_max` times (Kite
  allows 25 modifications per order). After that it alerts and stops chasing.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.models import Instrument, Order, OrderRequest, OrderType, Side, Tick

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OptionOrderRules:
    enabled: bool = True
    buffer_ticks: int = 2
    exit_buffer_pct: float = 2.0
    chase_after_sec: float = 5.0
    chase_max: int = 10


def limit_price(
    inst: Instrument, side: Side, quote: Tick, *, is_exit: bool, step: int, rules: OptionOrderRules
) -> float | None:
    """Marketable limit price for attempt `step` (0 = first placement)."""
    if side is Side.BUY:
        base = quote.ask or quote.ltp
        if not base:
            return None
        px = (
            base * (1 + rules.exit_buffer_pct * (step + 1) / 100)
            if is_exit
            else base + inst.tick_size * rules.buffer_ticks * (step + 1)
        )
    else:
        base = quote.bid or quote.ltp
        if not base:
            return None
        px = (
            base * (1 - rules.exit_buffer_pct * (step + 1) / 100)
            if is_exit
            else base - inst.tick_size * rules.buffer_ticks * (step + 1)
        )
    return max(inst.round_price(px), inst.tick_size)


def prepare(
    req: OrderRequest, quote: Tick | None, *, is_exit: bool, rules: OptionOrderRules
) -> tuple[OrderRequest, bool]:
    """Returns (request to send, whether it was converted to a chased LIMIT)."""
    if (
        not rules.enabled
        or not req.instrument.is_option
        or req.order_type is not OrderType.MARKET
        or req.protective
        or quote is None
    ):
        return req, False
    px = limit_price(req.instrument, req.side, quote, is_exit=is_exit, step=0, rules=rules)
    if px is None:
        return req, False
    return dataclasses.replace(req, order_type=OrderType.LIMIT, price=px), True


@dataclass
class _Chase:
    is_exit: bool
    last: datetime
    attempts: int = 0


class Chaser:
    def __init__(
        self,
        rules: OptionOrderRules,
        quote: Callable[[int], Tick | None],
        modify: Callable[[str, float], None],
        alert: Callable[[str], None],
    ) -> None:
        self._rules = rules
        self._quote = quote
        self._modify = modify
        self._alert = alert
        self._tracked: dict[str, _Chase] = {}

    def track(self, order: Order, *, is_exit: bool, now: datetime) -> None:
        self._tracked[order.client_order_id] = _Chase(is_exit, now)

    def tracked(self) -> set[str]:
        return set(self._tracked)

    def on_time(self, now: datetime, orders: dict[str, Order]) -> None:
        after = timedelta(seconds=self._rules.chase_after_sec)
        for cid, st in list(self._tracked.items()):
            order = orders.get(cid)
            if order is None or order.status.is_terminal:
                del self._tracked[cid]
                continue
            if now - st.last < after:
                continue
            st.last = now
            if st.attempts >= self._rules.chase_max:
                del self._tracked[cid]
                self._alert(
                    f"Option order {order.request.instrument.tradingsymbol} still open after "
                    f"{st.attempts} re-prices; stopped chasing"
                )
                continue
            quote = self._quote(order.request.instrument.token)
            if quote is None:
                continue
            st.attempts += 1
            px = limit_price(
                order.request.instrument,
                order.request.side,
                quote,
                is_exit=st.is_exit,
                step=st.attempts,
                rules=self._rules,
            )
            if px is not None and px != order.request.price:
                log.info(
                    "chasing option order",
                    extra={"client_order_id": cid, "price": px, "attempt": st.attempts},
                )
                self._modify(cid, px)
