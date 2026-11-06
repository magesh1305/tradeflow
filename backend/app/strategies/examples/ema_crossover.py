"""EMA crossover on the nearest NIFTY future.

Long when the fast EMA is above the slow EMA, short when below. On a crossover
it exits the current position and then enters the other way. No new entries
after `last_entry_time`; the engine's square-off flattens at end of day.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.models import Candle, Instrument, Product
from app.strategies.base import Strategy
from app.strategies.registry import register


class EmaParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    underlying: str = "NIFTY"
    interval_minutes: int = Field(default=5, ge=1, le=60)
    fast: int = Field(default=9, ge=2)
    slow: int = Field(default=21, ge=3)
    lots: int = Field(default=1, ge=1)
    last_entry_time: time = time(15, 0)

    @model_validator(mode="after")
    def _fast_below_slow(self) -> EmaParams:
        if self.fast >= self.slow:
            raise ValueError("fast must be smaller than slow")
        return self


def ema_step(prev: float | None, price: float, period: int) -> float:
    if prev is None:
        return price
    k = 2 / (period + 1)
    return price * k + prev * (1 - k)


@register
class EmaCrossover(Strategy):
    name = "ema_crossover"
    Params = EmaParams
    product = Product.MIS

    def on_start(self) -> None:
        p: EmaParams = self.params
        self.future: Instrument = self.ctx.instruments.nearest_future(p.underlying, self.ctx.today)
        self.fast: float | None = None
        self.slow: float | None = None
        self.bars = 0
        self.signal = 0  # +1 long, -1 short, 0 flat/undecided
        self.ctx.subscribe_candles(self.future.token, p.interval_minutes * 60)
        self.ctx.log.info("trading %s", self.future.tradingsymbol)

    def on_candle(self, candle: Candle) -> None:
        p: EmaParams = self.params
        self.fast = ema_step(self.fast, candle.close, p.fast)
        self.slow = ema_step(self.slow, candle.close, p.slow)
        self.bars += 1
        if self.bars < p.slow:
            return  # warm-up
        new_signal = 1 if self.fast > self.slow else -1
        if new_signal == self.signal:
            return
        self.signal = new_signal
        if self.ctx.open_orders():
            return  # let in-flight orders settle first
        if self.position(self.future):
            self.exit_position(self.future, reason="ema cross exit")
        if self.ctx.now().time() < p.last_entry_time:
            qty = self.lots(self.future, p.lots)
            if new_signal > 0:
                self.buy(self.future, qty, reason="ema cross long")
            else:
                self.sell(self.future, qty, reason="ema cross short")
