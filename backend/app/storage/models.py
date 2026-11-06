"""SQLAlchemy ORM tables. The schema itself is owned by Alembic migrations;
keep these models and `migrations/versions/` in sync."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


class OrderRow(Base):
    """Latest state of every order (one row per client order id)."""

    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(32), index=True)
    trading_date: Mapped[date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(8))
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    leg_group_id: Mapped[str | None] = mapped_column(String(32), index=True)
    exchange: Mapped[str] = mapped_column(String(8))
    tradingsymbol: Mapped[str] = mapped_column(String(64))
    instrument_token: Mapped[int] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[str] = mapped_column(String(8))
    product: Mapped[str] = mapped_column(String(8))
    price: Mapped[float | None] = mapped_column(Float)
    trigger_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20))
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_price: Mapped[float] = mapped_column(Float, default=0.0)
    status_message: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class OrderEventRow(Base):
    """Append-only history of every order state change (the audit journal)."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20))
    filled_quantity: Mapped[int] = mapped_column(Integer)
    average_price: Mapped[float] = mapped_column(Float)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any] | None]


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (Index("ix_fills_date_strategy", "trading_date", "strategy_id"),)

    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("orders.client_order_id"), index=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(32))
    trading_date: Mapped[date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(8))
    strategy_id: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8))
    tradingsymbol: Mapped[str] = mapped_column(String(64))
    instrument_token: Mapped[int] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    product: Mapped[str] = mapped_column(String(8))
    charges: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime]


class CandleRow(Base):
    """TimescaleDB hypertable partitioned on `ts`. Unique on (token, interval, ts)."""

    __tablename__ = "candles"

    instrument_token: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    interval: Mapped[str] = mapped_column(String(16), primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    oi: Mapped[int] = mapped_column(BigInteger, default=0)


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16))  # queued | running | done | failed
    strategy: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]]
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    metrics: Mapped[dict[str, Any] | None]
    equity_curve: Mapped[list[Any] | None]
    missing_data: Mapped[list[Any] | None]
    error: Mapped[str | None] = mapped_column(Text)


class DailyPnlRow(Base):
    """One row per (date, mode, strategy). strategy_id '__account__' holds the total."""

    __tablename__ = "daily_pnl"

    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    gross_pnl: Mapped[float] = mapped_column(Float)
    charges: Mapped[float] = mapped_column(Float)
    net_pnl: Mapped[float] = mapped_column(Float)
    trades: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime]


class LegGroupRow(Base):
    __tablename__ = "leg_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    trading_date: Mapped[date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(8))
    strategy_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    legs: Mapped[list[Any]]  # [{client_order_id, tradingsymbol, side, quantity, status}]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class InstrumentRow(Base):
    """Instrument master history. Kite reuses tokens after expiry and drops expired
    contracts from its dump, so backtests need the contract as it was on each day."""

    __tablename__ = "instruments"

    exchange: Mapped[str] = mapped_column(String(8), primary_key=True)
    tradingsymbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_token: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    instrument_type: Mapped[str] = mapped_column(String(8))
    lot_size: Mapped[int] = mapped_column(Integer)
    tick_size: Mapped[float] = mapped_column(Float)
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[float | None] = mapped_column(Float)
    first_seen: Mapped[date] = mapped_column(Date)
    last_seen: Mapped[date] = mapped_column(Date)
