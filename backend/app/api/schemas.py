"""Response/request models for the control API. The frontend's typed client
(frontend/src/api/types.ts) mirrors these; keep them in sync."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Pnl(BaseModel):
    gross: float
    net: float
    realized: float
    unrealized: float
    charges: float


class PositionOut(BaseModel):
    strategy_id: str
    exchange: str
    tradingsymbol: str
    label: str = ""  # e.g. "NIFTY 24000 Call · 10 Nov"
    lot_size: int = 1
    instrument_token: int
    product: str
    quantity: int
    average_price: float
    last_price: float | None
    pnl: Pnl
    protected_stop: float | None = None


class OrderOut(BaseModel):
    client_order_id: str
    broker_order_id: str | None
    mode: str
    strategy_id: str
    exchange: str
    tradingsymbol: str
    label: str = ""
    lot_size: int = 1
    side: str
    quantity: int
    order_type: str
    product: str
    price: float | None
    trigger_price: float | None
    status: str
    filled_quantity: int
    average_price: float
    status_message: str
    reason: str
    leg_group_id: str | None
    protective: bool = False
    created_at: datetime
    updated_at: datetime


class FillOut(BaseModel):
    fill_id: str
    client_order_id: str
    broker_order_id: str | None
    mode: str
    strategy_id: str
    exchange: str
    tradingsymbol: str
    label: str = ""
    lot_size: int = 1
    side: str
    quantity: int
    price: float
    product: str
    charges: float
    ts: datetime
    trading_date: date


class StrategyOut(BaseModel):
    id: str
    strategy: str
    title: str = ""  # plain-English name, e.g. "Trend follower"
    summary: str = ""
    risk_level: str = "medium"
    state: Literal["STOPPED", "RUNNING", "ERROR"]
    error: str
    pnl: Pnl
    open_positions: int
    open_orders: int


class StrategyDetail(StrategyOut):
    how_it_works: list[str] = []
    risk_note: str = ""
    params: dict[str, Any]
    params_schema: dict[str, Any]
    positions: list[PositionOut]
    subscribed_tokens: int


class FeedOut(BaseModel):
    source: str
    connected: bool
    detail: str


class KillSwitchOut(BaseModel):
    tripped: bool
    reason: str
    tripped_at: datetime | None


class StatusOut(BaseModel):
    time: datetime
    mode: Literal["paper", "live"]
    engine_running: bool
    blocked_reason: str
    feed: FeedOut
    kill_switch: KillSwitchOut
    pnl: Pnl
    positions: list[PositionOut]
    open_orders: list[OrderOut]
    strategies: list[StrategyOut]
    open_leg_groups: int
    journal: dict[str, Any]
    plan_submitted: bool = True  # False: strategies wait for today's plan (Settings)


class DailyPnlOut(BaseModel):
    trading_date: date
    mode: str
    strategy_id: str
    gross_pnl: float
    charges: float
    net_pnl: float
    trades: int
    live: bool = False  # today's row, not yet written by the end-of-day job


class RiskOut(BaseModel):
    limits: dict[str, Any]
    usage: dict[str, Any]
    kill_switch: KillSwitchOut
    margin: dict[str, Any] | None


class GreeksOut(BaseModel):
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


class OptionQuoteOut(BaseModel):
    tradingsymbol: str
    instrument_token: int
    ltp: float | None
    bid: float | None
    ask: float | None
    oi: int
    volume: int
    greeks: GreeksOut | None


class ChainRowOut(BaseModel):
    strike: float
    is_atm: bool
    call: OptionQuoteOut | None
    put: OptionQuoteOut | None


class OptionChainOut(BaseModel):
    underlying: str
    expiry: date | None
    expiries: list[date]
    spot: float | None
    rows: list[ChainRowOut]
    note: str = ""


class ParamsIn(BaseModel):
    params: dict[str, Any]


class KillSwitchIn(BaseModel):
    reason: str = Field(default="manual (dashboard)", max_length=200)


class BacktestIn(BaseModel):
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    start: date
    end: date
    interval: Literal[
        "minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute"
    ] = "minute"


class BacktestRunOut(BaseModel):
    id: int
    status: str
    strategy: str
    params: dict[str, Any]
    start_date: date
    end_date: date
    created_at: datetime
    finished_at: datetime | None
    metrics: dict[str, Any] | None = None
    error: str | None = None


class BacktestDetail(BacktestRunOut):
    equity_curve: list[dict[str, Any]] | None = None
    missing_data: list[dict[str, Any]] | None = None
