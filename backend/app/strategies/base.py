"""Strategy base class and the context strategies use to reach the engine.

Strategies never talk to a broker. They see market data, time, positions and
orders only through `StrategyContext`, and they place orders only through the
helpers here, which go through the engine and the RiskManager. That's why the
same strategy code runs unchanged in backtest, paper and live.

Hooks run on the engine's event loop and must not block. An exception in any
hook disables that strategy (other strategies keep running).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.core.models import (
    Candle,
    Fill,
    Instrument,
    Order,
    OrderRequest,
    OrderType,
    Product,
    Side,
    Tick,
)
from app.data.instruments import InstrumentStore
from app.engine.positions import Position

if TYPE_CHECKING:
    from app.data.option_chain import OptionChainService
    from app.engine.legs import LegGroup, LegSpec


class StrategyContext(Protocol):
    strategy_id: str
    instruments: InstrumentStore
    log: logging.Logger

    def now(self) -> datetime: ...
    @property
    def today(self) -> date: ...
    def ltp(self, token: int) -> float | None: ...
    def tick(self, token: int) -> Tick | None: ...
    def positions(self) -> list[Position]: ...
    def open_orders(self) -> list[Order]: ...
    def subscribe(self, tokens: list[int]) -> None: ...
    def subscribe_candles(self, token: int, interval_seconds: int) -> None: ...
    def submit(self, req: OrderRequest) -> Order: ...
    def cancel(self, client_order_id: str) -> None: ...
    @property
    def option_chain(self) -> OptionChainService: ...
    def place_legs(
        self, specs: list[LegSpec], product: Product, timeout_sec: float | None = None
    ) -> LegGroup: ...
    def protect(self, instrument: Instrument, stop_price: float, product: Product) -> None: ...
    def unprotect(self, instrument: Instrument) -> None: ...


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Strategy:
    """Subclass, set `name` and `Params`, decorate with `@register`."""

    name: ClassVar[str] = ""
    # Plain-English description for the dashboard (for people new to trading).
    title: ClassVar[str] = ""
    summary: ClassVar[str] = ""
    how_it_works: ClassVar[tuple[str, ...]] = ()
    risk_level: ClassVar[str] = "medium"  # "low" | "medium" | "high"
    risk_note: ClassVar[str] = ""
    Params: ClassVar[type[BaseModel]] = NoParams
    product: ClassVar[Product] = Product.MIS

    def __init__(self, strategy_id: str, params: Mapping[str, Any] | None = None) -> None:
        self.id = strategy_id
        self.params: Any = self.Params.model_validate(dict(params or {}))
        self._ctx: StrategyContext | None = None

    # --- wiring --------------------------------------------------------------------

    def bind(self, ctx: StrategyContext) -> None:
        self._ctx = ctx

    @property
    def ctx(self) -> StrategyContext:
        if self._ctx is None:
            raise RuntimeError(f"strategy {self.id} is not bound to an engine")
        return self._ctx

    # --- hooks (override as needed) --------------------------------------------------

    def on_start(self) -> None: ...
    def on_tick(self, tick: Tick) -> None: ...
    def on_candle(self, candle: Candle) -> None: ...
    def on_order(self, order: Order) -> None: ...
    def on_fill(self, fill: Fill) -> None: ...
    def on_legs(self, group: LegGroup) -> None: ...
    def on_stop(self) -> None: ...

    # --- helpers ---------------------------------------------------------------------

    @staticmethod
    def lots(instrument: Instrument, n: int) -> int:
        """Quantity in units for `n` lots."""
        return n * instrument.lot_size

    def position(self, instrument: Instrument) -> int:
        """Net signed quantity this strategy holds in `instrument` (all products)."""
        return sum(
            p.quantity for p in self.ctx.positions() if p.instrument.token == instrument.token
        )

    def _order(
        self,
        side: Side,
        instrument: Instrument,
        quantity: int,
        *,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        trigger_price: float | None = None,
        product: Product | None = None,
        reason: str = "",
    ) -> Order:
        return self.ctx.submit(
            OrderRequest(
                strategy_id=self.id,
                instrument=instrument,
                side=side,
                quantity=quantity,
                order_type=order_type,
                product=product or self.product,
                price=price,
                trigger_price=trigger_price,
                reason=reason,
            )
        )

    def buy(self, instrument: Instrument, quantity: int, **kw: Any) -> Order:
        return self._order(Side.BUY, instrument, quantity, **kw)

    def sell(self, instrument: Instrument, quantity: int, **kw: Any) -> Order:
        return self._order(Side.SELL, instrument, quantity, **kw)

    def exit_position(self, instrument: Instrument, reason: str = "exit") -> list[Order]:
        """Flatten this strategy's position in `instrument`, keeping each product.
        Releases the exchange-side stop first so it can't race the exit. Quantity
        already covered by pending exit orders is not exited twice."""
        self.ctx.unprotect(instrument)
        orders: list[Order] = []
        pending = self.ctx.open_orders()
        for pos in self.ctx.positions():
            if pos.instrument.token != instrument.token or pos.quantity == 0:
                continue
            side = Side.SELL if pos.quantity > 0 else Side.BUY
            in_flight = sum(
                o.pending_quantity
                for o in pending
                if o.request.instrument.token == instrument.token
                and o.request.product == pos.product
                and o.request.side == side
                and not o.request.protective
            )
            qty = abs(pos.quantity) - in_flight
            if qty > 0:
                orders.append(
                    self._order(side, pos.instrument, qty, product=pos.product, reason=reason)
                )
        return orders

    # --- F&O helpers ------------------------------------------------------------------

    def place_legs(
        self, *specs: LegSpec, product: Product | None = None, timeout_sec: float | None = None
    ) -> LegGroup:
        """Place several legs as one group: all fill, or the filled ones are unwound."""
        return self.ctx.place_legs(list(specs), product or self.product, timeout_sec)

    def protect(
        self, instrument: Instrument, stop_price: float, product: Product | None = None
    ) -> None:
        """Keep an exchange-side SL order for this position at `stop_price` (call again
        to trail). Keep your in-code stop too: this is the second layer."""
        self.ctx.protect(instrument, stop_price, product or self.product)

    def atm(self, underlying: str, expiry: date | None = None) -> float:
        return self.ctx.option_chain.atm(underlying, expiry)

    def select_by_delta(
        self,
        underlying: str,
        option_type: Literal["CE", "PE"],
        delta: float,
        expiry: date | None = None,
    ) -> Instrument:
        return self.ctx.option_chain.select_by_delta(underlying, option_type, delta, expiry)

    def select_by_premium(
        self,
        underlying: str,
        option_type: Literal["CE", "PE"],
        premium: float,
        expiry: date | None = None,
    ) -> Instrument:
        return self.ctx.option_chain.select_by_premium(underlying, option_type, premium, expiry)

    def subscribe_chain(self, underlying: str, expiry: date | None = None, width: int = 10) -> None:
        """Subscribe `width` strikes either side of ATM (calls and puts)."""
        self.ctx.subscribe(self.ctx.option_chain.tokens_around_atm(underlying, expiry, width))

    def roll(self, instrument: Instrument, reason: str = "roll") -> LegGroup | None:
        """Move this strategy's position in `instrument` to the next expiry, as one leg
        group (exit current + enter next, same size). Rolled positions are NRML."""
        from app.core.models import Side  # noqa: PLC0415
        from app.engine.legs import LegSpec  # noqa: PLC0415

        target = self.ctx.instruments.next_contract(instrument)
        specs: list[LegSpec] = []
        for pos in self.ctx.positions():
            if pos.instrument.token != instrument.token or pos.quantity == 0:
                continue
            side = Side.BUY if pos.quantity > 0 else Side.SELL
            specs.append(LegSpec(instrument, side.opposite, abs(pos.quantity), product=pos.product))
            specs.append(LegSpec(target, side, abs(pos.quantity), product=Product.NRML))
        if not specs:
            return None
        self.ctx.unprotect(instrument)
        self.ctx.log.info(
            "rolling %s -> %s (%s)", instrument.tradingsymbol, target.tradingsymbol, reason
        )
        return self.place_legs(*specs, product=Product.NRML)

    def exit_all(self, reason: str = "exit all") -> list[Order]:
        orders: list[Order] = []
        seen: set[int] = set()
        for pos in self.ctx.positions():
            if pos.quantity and pos.instrument.token not in seen:
                seen.add(pos.instrument.token)
                orders.extend(self.exit_position(pos.instrument, reason))
        return orders
