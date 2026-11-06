"""Position book: signed quantity, average price, realized and unrealized P&L
per (strategy, instrument, product). Handles adds, partial exits and flips.

Product is part of the key because a square-off must use the position's own
product (MIS vs NRML).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.core.models import Fill, Instrument, Product

PositionKey = tuple[str, int, Product]  # (strategy_id, token, product)


@dataclass(slots=True)
class Position:
    strategy_id: str
    instrument: Instrument
    product: Product
    quantity: int = 0  # signed: + long, - short
    average_price: float = 0.0
    realized_pnl: float = 0.0
    last_price: float | None = None
    buy_quantity: int = 0
    sell_quantity: int = 0
    buy_value: float = 0.0
    sell_value: float = 0.0
    charges: float = 0.0
    fills: int = field(default=0)

    @property
    def key(self) -> PositionKey:
        return (self.strategy_id, self.instrument.token, self.product)

    @property
    def is_open(self) -> bool:
        return self.quantity != 0

    @property
    def unrealized_pnl(self) -> float:
        if self.quantity == 0 or self.last_price is None:
            return 0.0
        return (self.last_price - self.average_price) * self.quantity

    @property
    def gross_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.charges

    def apply(self, fill: Fill, charges: float = 0.0) -> float:
        """Apply a fill; returns the realized P&L produced by this fill."""
        qty = fill.signed_quantity
        price = fill.price
        realized = 0.0
        self.fills += 1
        self.charges += charges
        if fill.quantity > 0:
            if qty > 0:
                self.buy_quantity += fill.quantity
                self.buy_value += fill.quantity * price
            else:
                self.sell_quantity += fill.quantity
                self.sell_value += fill.quantity * price

        if self.quantity == 0 or (self.quantity > 0) == (qty > 0):
            # Opening or adding: weighted average price.
            new_qty = self.quantity + qty
            self.average_price = (self.average_price * abs(self.quantity) + price * abs(qty)) / abs(
                new_qty
            )
            self.quantity = new_qty
        else:
            closing = min(abs(qty), abs(self.quantity))
            direction = 1 if self.quantity > 0 else -1
            realized = (price - self.average_price) * closing * direction
            self.realized_pnl += realized
            remaining = qty + closing * direction  # what is left after closing
            self.quantity += closing * -direction
            if self.quantity == 0:
                self.average_price = 0.0
            if remaining != 0:
                # Flip: the rest opens a new position at the fill price.
                self.quantity = remaining
                self.average_price = price
        if self.last_price is None:
            self.last_price = price
        return realized


@dataclass(frozen=True, slots=True)
class PnlSummary:
    realized: float
    unrealized: float
    charges: float

    @property
    def gross(self) -> float:
        return self.realized + self.unrealized

    @property
    def net(self) -> float:
        return self.gross - self.charges


class PositionBook:
    def __init__(self) -> None:
        self._positions: dict[PositionKey, Position] = {}

    def apply_fill(self, fill: Fill, charges: float = 0.0) -> Position:
        key = (fill.strategy_id, fill.instrument.token, fill.product)
        pos = self._positions.get(key)
        if pos is None:
            pos = Position(fill.strategy_id, fill.instrument, fill.product)
            self._positions[key] = pos
        pos.apply(fill, charges)
        return pos

    def mark(self, token: int, price: float) -> None:
        for pos in self._positions.values():
            if pos.instrument.token == token:
                pos.last_price = price

    def get(self, strategy_id: str, token: int, product: Product) -> Position | None:
        return self._positions.get((strategy_id, token, product))

    def net_quantity(self, strategy_id: str, token: int) -> int:
        return sum(
            p.quantity
            for p in self._positions.values()
            if p.strategy_id == strategy_id and p.instrument.token == token
        )

    def positions(self, strategy_id: str | None = None) -> list[Position]:
        return [
            p
            for p in self._positions.values()
            if strategy_id is None or p.strategy_id == strategy_id
        ]

    def open_positions(self, strategy_id: str | None = None) -> list[Position]:
        return [p for p in self.positions(strategy_id) if p.is_open]

    def pnl(self, strategy_id: str | None = None) -> PnlSummary:
        return _summarize(self.positions(strategy_id))

    def pnl_by_strategy(self) -> dict[str, PnlSummary]:
        ids = {p.strategy_id for p in self._positions.values()}
        return {sid: self.pnl(sid) for sid in sorted(ids)}

    def clear(self) -> None:
        self._positions.clear()


def _summarize(positions: Iterable[Position]) -> PnlSummary:
    r = u = c = 0.0
    for p in positions:
        r += p.realized_pnl
        u += p.unrealized_pnl
        c += p.charges
    return PnlSummary(realized=r, unrealized=u, charges=c)
