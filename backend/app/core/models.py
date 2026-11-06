"""Core domain models shared by every layer (feeds, engine, risk, execution, storage).

Prices are floats in INR, matching what Kite returns; they are rounded to the
instrument tick size where an exchange-facing price is produced.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from app.core.clock import now_ist

CLIENT_ORDER_ID_LEN = 16
_ID_ALPHABET = string.ascii_letters + string.digits


def new_client_order_id() -> str:
    """16-char alphanumeric id. Sent to Kite as the order `tag` (alphanumeric, max 20)."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(CLIENT_ORDER_ID_LEN))


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class Product(StrEnum):
    MIS = "MIS"  # intraday
    NRML = "NRML"  # carried F&O positions


class OrderStatus(StrEnum):
    PENDING = "PENDING"  # created locally, not yet acknowledged by the broker
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset({OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED})


class Exchange(StrEnum):
    NSE = "NSE"
    NFO = "NFO"


class InstrumentType(StrEnum):
    EQ = "EQ"
    FUT = "FUT"
    CE = "CE"
    PE = "PE"
    INDEX = "INDEX"


@dataclass(frozen=True, slots=True)
class Instrument:
    token: int
    tradingsymbol: str
    exchange: Exchange
    name: str  # underlying, e.g. "NIFTY"
    instrument_type: InstrumentType
    lot_size: int
    tick_size: float
    expiry: date | None = None
    strike: float | None = None

    @property
    def key(self) -> str:
        """Kite-style instrument key, e.g. 'NFO:NIFTY25NOVFUT'."""
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def is_option(self) -> bool:
        return self.instrument_type in (InstrumentType.CE, InstrumentType.PE)

    def round_price(self, price: float) -> float:
        """Round to the nearest valid tick."""
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 2)


@dataclass(slots=True)
class Tick:
    token: int
    ts: datetime  # exchange timestamp, IST-aware
    ltp: float
    volume: int = 0
    oi: int = 0
    bid: float | None = None
    ask: float | None = None
    bid_qty: int = 0
    ask_qty: int = 0


@dataclass(slots=True)
class Candle:
    token: int
    interval: int  # seconds
    ts: datetime  # candle start, IST-aware
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    oi: int = 0


@dataclass(slots=True)
class OrderRequest:
    strategy_id: str
    instrument: Instrument
    side: Side
    quantity: int  # units (lots * lot_size), never lots
    order_type: OrderType = OrderType.MARKET
    product: Product = Product.MIS
    price: float | None = None
    trigger_price: float | None = None
    leg_group_id: str | None = None
    reason: str = ""
    client_order_id: str = field(default_factory=new_client_order_id)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type in (OrderType.LIMIT, OrderType.SL) and self.price is None:
            raise ValueError(f"{self.order_type} order requires a price")
        if self.order_type in (OrderType.SL, OrderType.SL_M) and self.trigger_price is None:
            raise ValueError(f"{self.order_type} order requires a trigger_price")
        if len(self.client_order_id) != CLIENT_ORDER_ID_LEN or not self.client_order_id.isalnum():
            raise ValueError("client_order_id must be 16 alphanumeric characters")

    @property
    def signed_quantity(self) -> int:
        return self.side.sign * self.quantity


@dataclass(slots=True)
class Order:
    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    filled_quantity: int = 0
    average_price: float = 0.0
    status_message: str = ""
    created_at: datetime = field(default_factory=now_ist)
    updated_at: datetime = field(default_factory=now_ist)

    @property
    def client_order_id(self) -> str:
        return self.request.client_order_id

    @property
    def pending_quantity(self) -> int:
        if self.status.is_terminal:
            return 0
        return self.request.quantity - self.filled_quantity


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    client_order_id: str
    strategy_id: str
    instrument: Instrument
    side: Side
    quantity: int
    price: float
    ts: datetime
    product: Product
    broker_order_id: str | None = None

    @property
    def signed_quantity(self) -> int:
        return self.side.sign * self.quantity
