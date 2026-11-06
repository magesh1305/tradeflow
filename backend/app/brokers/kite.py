"""Async wrapper around the synchronous `kiteconnect.KiteConnect` SDK.

- Every SDK call runs in a worker thread (`asyncio.to_thread`).
- Order calls (place/modify/cancel) go through an order rate limiter kept below
  Kite's 10/sec and 400/min limits. Historical calls stay below 3/sec, other
  calls below 10/sec.
- MARKET and SL-M orders always carry `market_protection` (Kite requires it for
  API orders). LIMIT and SL orders never do.
- Order placement is never retried automatically: a timeout could mean the order
  went through, and a blind retry could double the position. Reconciliation
  (Phase 4) resolves unknown outcomes using the order tag.
- `orders_enabled=False` (the factory sets this in paper mode) makes every order
  call raise, so a paper session can never reach Kite's order endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from app.core.clock import to_ist
from app.core.models import OrderRequest, OrderType
from app.core.ratelimit import RateLimiter

log = logging.getLogger(__name__)

T = TypeVar("T")

VARIETY_REGULAR = "regular"
VALIDITY_DAY = "DAY"
NEEDS_MARKET_PROTECTION = frozenset({OrderType.MARKET, OrderType.SL_M})


# Kite exception types that mean the request was definitely refused. Anything else
# (NetworkException, GeneralException/5xx, timeouts, connection errors) leaves the
# outcome unknown: an order may or may not have been placed.
DEFINITE_FAILURES = frozenset(
    {"InputException", "OrderException", "PermissionException", "TokenException", "OrdersDisabled"}
)


class BrokerError(RuntimeError):
    """Any failure reported by Kite or the network. `kind` is the original exception name."""

    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def outcome_unknown(self) -> bool:
        """True when the request may have reached Kite (never retry; reconcile instead)."""
        return self.kind not in DEFINITE_FAILURES


class OrdersDisabledError(BrokerError):
    """Order endpoint called while orders are disabled (paper mode)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "OrdersDisabled")


class SessionExpiredError(BrokerError):
    """Kite rejected the access token (expired or logged out)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "TokenException")


def _is_token_error(exc: Exception) -> bool:
    # kiteconnect.exceptions.TokenException; matched by name so tests need not import it.
    return type(exc).__name__ == "TokenException"


class KiteBroker:
    def __init__(
        self,
        kite: Any,
        *,
        orders_enabled: bool,
        market_protection: float = -1,
        order_limiter: RateLimiter | None = None,
        data_limiter: RateLimiter | None = None,
        historical_limiter: RateLimiter | None = None,
        on_session_expired: Callable[[], None] | None = None,
    ) -> None:
        self._kite = kite
        self._orders_enabled = orders_enabled
        self._market_protection = market_protection
        self._order_limiter = order_limiter or RateLimiter([(8, 1.0), (350, 60.0)])
        self._data_limiter = data_limiter or RateLimiter([(8, 1.0)])
        self._historical_limiter = historical_limiter or RateLimiter([(2, 1.0)])
        self._on_session_expired = on_session_expired

    @property
    def orders_enabled(self) -> bool:
        return self._orders_enabled

    async def _call(self, limiter: RateLimiter, fn: Callable[..., T], *args: Any, **kw: Any) -> T:
        await limiter.acquire()
        try:
            return await asyncio.to_thread(fn, *args, **kw)
        except Exception as exc:
            if _is_token_error(exc):
                if self._on_session_expired:
                    self._on_session_expired()
                raise SessionExpiredError(str(exc)) from exc
            kind = type(exc).__name__
            raise BrokerError(f"{kind}: {exc}", kind) from exc

    def _require_orders(self) -> None:
        if not self._orders_enabled:
            raise OrdersDisabledError("order endpoints are disabled (paper mode)")

    # --- orders ---------------------------------------------------------------

    def order_params(self, req: OrderRequest) -> dict[str, Any]:
        """Translate an OrderRequest into Kite `place_order` keyword arguments."""
        inst = req.instrument
        params: dict[str, Any] = {
            "variety": VARIETY_REGULAR,
            "exchange": str(inst.exchange),
            "tradingsymbol": inst.tradingsymbol,
            "transaction_type": str(req.side),
            "quantity": req.quantity,
            "product": str(req.product),
            "order_type": str(req.order_type),
            "validity": VALIDITY_DAY,
            "tag": req.client_order_id,
        }
        if req.price is not None and req.order_type in (OrderType.LIMIT, OrderType.SL):
            params["price"] = inst.round_price(req.price)
        if req.trigger_price is not None and req.order_type in (OrderType.SL, OrderType.SL_M):
            params["trigger_price"] = inst.round_price(req.trigger_price)
        if req.order_type in NEEDS_MARKET_PROTECTION:
            params["market_protection"] = self._market_protection
        return params

    async def place_order(self, req: OrderRequest) -> str:
        """Place a regular order. Returns the Kite order id."""
        self._require_orders()
        params = self.order_params(req)
        order_id = await self._call(self._order_limiter, self._kite.place_order, **params)
        log.info(
            "order placed",
            extra={"client_order_id": req.client_order_id, "order_id": order_id},
        )
        return str(order_id)

    async def place_autoslice_order(self, req: OrderRequest) -> dict[str, Any]:
        """Place with Kite auto-slicing (above freeze quantity, max 10 slices).

        Returns the raw response: {"order_id": parent, "children": [{"order_id"} | {"error"}]}.
        """
        self._require_orders()
        params = self.order_params(req)
        resp = await self._call(self._order_limiter, self._kite.place_autoslice_order, **params)
        return dict(resp)

    async def modify_order(
        self,
        order_id: str,
        *,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        order_type: OrderType | None = None,
    ) -> str:
        self._require_orders()
        kw: dict[str, Any] = {"variety": VARIETY_REGULAR, "order_id": order_id}
        if quantity is not None:
            kw["quantity"] = quantity
        if price is not None:
            kw["price"] = price
        if trigger_price is not None:
            kw["trigger_price"] = trigger_price
        if order_type is not None:
            kw["order_type"] = str(order_type)
            if order_type in NEEDS_MARKET_PROTECTION:
                kw["market_protection"] = self._market_protection
        return str(await self._call(self._order_limiter, self._kite.modify_order, **kw))

    async def cancel_order(self, order_id: str) -> str:
        self._require_orders()
        return str(
            await self._call(
                self._order_limiter,
                self._kite.cancel_order,
                variety=VARIETY_REGULAR,
                order_id=order_id,
            )
        )

    # --- account state --------------------------------------------------------

    async def orders(self) -> list[dict[str, Any]]:
        return list(await self._call(self._data_limiter, self._kite.orders))

    async def order_history(self, order_id: str) -> list[dict[str, Any]]:
        return list(await self._call(self._data_limiter, self._kite.order_history, order_id))

    async def order_trades(self, order_id: str) -> list[dict[str, Any]]:
        return list(await self._call(self._data_limiter, self._kite.order_trades, order_id))

    async def positions(self) -> dict[str, Any]:
        """{"net": [...], "day": [...]}"""
        return dict(await self._call(self._data_limiter, self._kite.positions))

    async def margins(self, segment: str | None = None) -> dict[str, Any]:
        return dict(await self._call(self._data_limiter, self._kite.margins, segment))

    async def order_margins(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(await self._call(self._data_limiter, self._kite.order_margins, orders))

    async def basket_margins(
        self, orders: list[dict[str, Any]], consider_positions: bool = True
    ) -> dict[str, Any]:
        return dict(
            await self._call(
                self._data_limiter,
                self._kite.basket_order_margins,
                orders,
                consider_positions=consider_positions,
            )
        )

    # --- market data ----------------------------------------------------------

    async def instruments(self, exchange: str) -> list[dict[str, Any]]:
        return list(await self._call(self._data_limiter, self._kite.instruments, exchange))

    async def historical_data(
        self,
        instrument_token: int,
        from_dt: datetime,
        to_dt: datetime,
        interval: str,
        *,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, Any]]:
        # The SDK formats datetimes as naive "YYYY-mm-dd HH:MM:SS"; Kite interprets IST.
        return list(
            await self._call(
                self._historical_limiter,
                self._kite.historical_data,
                instrument_token,
                to_ist(from_dt).replace(tzinfo=None),
                to_ist(to_dt).replace(tzinfo=None),
                interval,
                continuous=continuous,
                oi=oi,
            )
        )
