"""Intraday ATM short straddle (disabled by default; sells options = unlimited risk).

At `entry_time`, sell the ATM call and put of the nearest expiry as ONE leg group.
If either leg fails, the engine unwinds the other, so the strategy is never left
holding half a straddle. After each leg fills, an exchange-side SL order protects
it at `stop_loss_pct` above the entry premium. The same stop also runs in code as
a second layer. Exit everything at `exit_time`; the engine's square-off is the backstop.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field

from app.core.models import Fill, Instrument, Product, Side, Tick
from app.engine.legs import LegGroup, LegGroupStatus, LegSpec
from app.strategies.base import Strategy
from app.strategies.registry import register


class StraddleParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    underlying: str = "NIFTY"
    entry_time: time = time(9, 20)
    exit_time: time = time(15, 0)
    lots: int = Field(default=1, ge=1)
    stop_loss_pct: float = Field(default=30.0, gt=0, le=500)
    exchange_stops: bool = True


@register
class ShortStraddle(Strategy):
    name = "short_straddle"
    Params = StraddleParams
    product = Product.MIS

    def on_start(self) -> None:
        p: StraddleParams = self.params
        self.spot: Instrument = self.ctx.instruments.spot(p.underlying)
        self.legs: list[Instrument] = []
        self.entry_price: dict[int, float] = {}
        self.stopped: set[int] = set()
        self.group: LegGroup | None = None
        self.done = False
        self.ctx.subscribe([self.spot.token])

    def on_tick(self, tick: Tick) -> None:
        p: StraddleParams = self.params
        now = self.ctx.now().time()
        if self.done:
            return
        if now >= p.exit_time:
            if self.group is not None:
                self.exit_all(reason="straddle time exit")
            self.done = True
            return
        if not self.legs and now >= p.entry_time:
            self._select_legs()
            return
        if self.legs and self.group is None:
            if all(self.ctx.tick(leg.token) for leg in self.legs):
                qty = self.lots(self.legs[0], p.lots)
                self.group = self.place_legs(*(LegSpec(leg, Side.SELL, qty) for leg in self.legs))
            return
        # In-code stop: the second layer behind the exchange-side SL order.
        if tick.token in self.entry_price and tick.token not in self.stopped:
            stop = self._stop_price(tick.token)
            if tick.ltp >= stop:
                leg = next(leg for leg in self.legs if leg.token == tick.token)
                self.stopped.add(tick.token)
                self.exit_position(leg, reason=f"leg stop {tick.ltp:.2f} >= {stop:.2f}")

    def _stop_price(self, token: int) -> float:
        p: StraddleParams = self.params
        return self.entry_price[token] * (1 + p.stop_loss_pct / 100)

    def _select_legs(self) -> None:
        p: StraddleParams = self.params
        if self.ctx.ltp(self.spot.token) is None:
            return
        store = self.ctx.instruments
        expiry = store.nearest_expiry(p.underlying, self.ctx.today)
        strike = self.atm(p.underlying, expiry)
        self.legs = [
            store.option(p.underlying, expiry, strike, "CE"),
            store.option(p.underlying, expiry, strike, "PE"),
        ]
        self.ctx.subscribe([leg.token for leg in self.legs])
        self.ctx.log.info("selected %s", [leg.tradingsymbol for leg in self.legs])

    def on_fill(self, fill: Fill) -> None:
        token = fill.instrument.token
        if fill.side is Side.SELL and token not in self.entry_price:
            self.entry_price[token] = fill.price
            if self.params.exchange_stops:
                self.protect(fill.instrument, self._stop_price(token))

    def on_legs(self, group: LegGroup) -> None:
        if group.status in (
            LegGroupStatus.REJECTED,
            LegGroupStatus.UNWOUND,
            LegGroupStatus.UNWIND_FAILED,
            LegGroupStatus.ABORTED,
        ):
            self.ctx.log.warning("straddle entry failed (%s): %s", group.status, group.error)
            self.done = True  # no re-entry today
