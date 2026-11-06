"""Pre-trade margin checks for opening orders and leg groups (exits are never checked).

- Paper: `EstimatedMargin`, a configurable estimate. Futures and short options block
  `paper_margin_pct` of notional (strike, or price for futures); long options block
  their premium. Available = `paper_capital` minus the estimate for open positions
  and pending entry orders. It answers synchronously.
- Live: `KiteMargin` asks Kite. Required margin comes from POST /margins/orders
  (`total`), or from POST /margins/basket for several legs (`final.total`, which
  includes the hedge benefit, falling back to `initial.total`). Available funds are
  `equity.net` from GET /user/margins. Any error fails CLOSED (the order is rejected).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from app.brokers.kite import BrokerError, KiteBroker
from app.core.models import Instrument, InstrumentType, Order, OrderRequest, Side, Tick
from app.engine.positions import Position

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarginResult:
    ok: bool
    required: float
    available: float
    reason: str = ""


class MarginChecker(Protocol):
    def check_now(
        self, reqs: list[OrderRequest], positions: list[Position], pending: list[Order]
    ) -> MarginResult | None:
        """Synchronous answer, or None if the check must run asynchronously."""

    async def check(self, reqs: list[OrderRequest]) -> MarginResult: ...

    async def funds(self, positions: list[Position], pending: list[Order]) -> dict[str, Any]:
        """Available / used margin for the dashboard."""


def _price(inst: Instrument, req_price: float | None, quote: Tick | None) -> float:
    if req_price:
        return req_price
    if quote is not None:
        return quote.ltp
    return 0.0


class EstimatedMargin:
    def __init__(
        self, capital: float, margin_pct: float, quote: Callable[[int], Tick | None]
    ) -> None:
        self._capital = capital
        self._pct = margin_pct
        self._quote = quote

    def estimate(self, inst: Instrument, side: Side, qty: int, price: float | None) -> float:
        px = _price(inst, price, self._quote(inst.token))
        if inst.is_option:
            if side is Side.BUY:
                return px * qty  # premium
            return (inst.strike or px) * qty * self._pct
        if inst.instrument_type in (InstrumentType.FUT, InstrumentType.EQ):
            return px * qty * self._pct
        return 0.0

    def _used(self, positions: Iterable[Position], pending: Iterable[Order]) -> float:
        used = 0.0
        for p in positions:
            if p.quantity:
                side = Side.BUY if p.quantity > 0 else Side.SELL
                used += self.estimate(p.instrument, side, abs(p.quantity), p.average_price)
        for o in pending:
            r = o.request
            if not r.protective:
                used += self.estimate(r.instrument, r.side, o.pending_quantity, r.price)
        return used

    def check_now(
        self, reqs: list[OrderRequest], positions: list[Position], pending: list[Order]
    ) -> MarginResult:
        required = sum(self.estimate(r.instrument, r.side, r.quantity, r.price) for r in reqs)
        available = self._capital - self._used(positions, pending)
        if required > available:
            return MarginResult(
                False,
                required,
                available,
                f"insufficient margin: need {required:,.0f}, "
                f"available {available:,.0f} (paper estimate)",
            )
        return MarginResult(True, required, available)

    async def check(self, reqs: list[OrderRequest]) -> MarginResult:
        return self.check_now(reqs, [], [])

    async def funds(self, positions: list[Position], pending: list[Order]) -> dict[str, Any]:
        used = self._used(positions, pending)
        return {
            "source": "paper estimate",
            "capital": self._capital,
            "used": used,
            "available": self._capital - used,
        }


class KiteMargin:
    def __init__(self, broker: KiteBroker) -> None:
        self._broker = broker

    def check_now(
        self, reqs: list[OrderRequest], positions: list[Position], pending: list[Order]
    ) -> MarginResult | None:
        return None  # always asynchronous

    async def funds(self, positions: list[Position], pending: list[Order]) -> dict[str, Any]:
        try:
            eq = await self._broker.margins("equity")
        except BrokerError as exc:
            return {"source": "kite", "error": str(exc)}
        return {
            "source": "kite",
            "available": float(eq.get("net", 0.0)),
            "used": float((eq.get("utilised") or {}).get("debits", 0.0)),
        }

    @staticmethod
    def _params(broker: KiteBroker, req: OrderRequest) -> dict[str, Any]:
        p = broker.order_params(req)
        keys = (
            "exchange",
            "tradingsymbol",
            "transaction_type",
            "variety",
            "product",
            "order_type",
            "quantity",
            "price",
            "trigger_price",
        )
        out = {k: p[k] for k in keys if k in p}
        out.setdefault("price", 0)
        out.setdefault("trigger_price", 0)
        return out

    async def check(self, reqs: list[OrderRequest]) -> MarginResult:
        try:
            funds = await self._broker.margins("equity")
            available = float(funds.get("net", 0.0))
            params = [self._params(self._broker, r) for r in reqs]
            if len(params) == 1:
                rows = await self._broker.order_margins(params)
                required = float(rows[0]["total"])
            else:
                basket = await self._broker.basket_margins(params, consider_positions=True)
                block = basket.get("final") or basket.get("initial") or {}
                required = float(block["total"])
        except (BrokerError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.error("margin check failed; rejecting", extra={"error": str(exc)})
            return MarginResult(False, 0.0, 0.0, f"margin check failed: {exc}")
        if required > available:
            return MarginResult(
                False,
                required,
                available,
                f"insufficient margin: need {required:,.0f}, available {available:,.0f}",
            )
        return MarginResult(True, required, available)
