"""Intraday ATM short straddle (disabled by default; sells options = unlimited risk).

At `entry_time`, sell the ATM call and put of the nearest expiry. Each leg has
its own stop: buy it back if its premium rises `stop_loss_pct` above the entry
price. Exit everything at `exit_time`. The engine's square-off is the backstop.

Phase 5 replaces the two separate entries with an atomic leg group and adds
exchange-side SL orders. Here the stop runs in code only.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field

from app.core.models import Fill, Instrument, Product, Side, Tick
from app.strategies.base import Strategy
from app.strategies.registry import register


class StraddleParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    underlying: str = "NIFTY"
    entry_time: time = time(9, 20)
    exit_time: time = time(15, 0)
    lots: int = Field(default=1, ge=1)
    stop_loss_pct: float = Field(default=30.0, gt=0, le=500)


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
        self.entered = False
        self.exited = False
        self.ctx.subscribe([self.spot.token])

    def on_tick(self, tick: Tick) -> None:
        p: StraddleParams = self.params
        now = self.ctx.now().time()
        if self.exited:
            return
        if now >= p.exit_time:
            if self.entered:
                self.exit_all(reason="straddle time exit")
            self.exited = True
            return
        if not self.legs and now >= p.entry_time:
            self._select_legs()
            return
        if self.legs and not self.entered:
            if all(self.ctx.ltp(leg.token) for leg in self.legs):
                for leg in self.legs:
                    self.sell(leg, self.lots(leg, p.lots), reason="straddle entry")
                self.entered = True
            return
        if tick.token in self.entry_price and tick.token not in self.stopped:
            stop = self.entry_price[tick.token] * (1 + p.stop_loss_pct / 100)
            if tick.ltp >= stop:
                leg = next(leg for leg in self.legs if leg.token == tick.token)
                self.stopped.add(tick.token)
                self.exit_position(leg, reason=f"leg stop {tick.ltp:.2f} >= {stop:.2f}")

    def _select_legs(self) -> None:
        p: StraddleParams = self.params
        spot = self.ctx.ltp(self.spot.token)
        if spot is None:
            return
        store = self.ctx.instruments
        expiry = store.nearest_expiry(p.underlying, self.ctx.today)
        strike = store.atm_strike(p.underlying, expiry, spot)
        self.legs = [
            store.option(p.underlying, expiry, strike, "CE"),
            store.option(p.underlying, expiry, strike, "PE"),
        ]
        self.ctx.subscribe([leg.token for leg in self.legs])
        self.ctx.log.info("selected %s", [leg.tradingsymbol for leg in self.legs])

    def on_fill(self, fill: Fill) -> None:
        if fill.side is Side.SELL and fill.instrument.token not in self.entry_price:
            self.entry_price[fill.instrument.token] = fill.price
