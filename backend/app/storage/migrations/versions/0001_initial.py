"""initial schema: orders, order_events, fills, candles (hypertable), backtest_runs,
daily_pnl, leg_groups

Revision ID: 0001
Revises:
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "orders",
        sa.Column("client_order_id", sa.String(16), primary_key=True),
        sa.Column("broker_order_id", sa.String(32), nullable=True),
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("leg_group_id", sa.String(32), nullable=True),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("tradingsymbol", sa.String(64), nullable=False),
        sa.Column("instrument_token", sa.BigInteger, nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("order_type", sa.String(8), nullable=False),
        sa.Column("product", sa.String(8), nullable=False),
        sa.Column("price", sa.Float, nullable=True),
        sa.Column("trigger_price", sa.Float, nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("filled_quantity", sa.Integer, nullable=False),
        sa.Column("average_price", sa.Float, nullable=False),
        sa.Column("status_message", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_orders_broker_order_id", "orders", ["broker_order_id"])
    op.create_index("ix_orders_trading_date", "orders", ["trading_date"])
    op.create_index("ix_orders_strategy_id", "orders", ["strategy_id"])
    op.create_index("ix_orders_leg_group_id", "orders", ["leg_group_id"])

    op.create_table(
        "order_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.String(16), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("filled_quantity", sa.Integer, nullable=False),
        sa.Column("average_price", sa.Float, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=True),
    )
    op.create_index("ix_order_events_client_order_id", "order_events", ["client_order_id"])

    op.create_table(
        "fills",
        sa.Column("fill_id", sa.String(64), primary_key=True),
        sa.Column(
            "client_order_id",
            sa.String(16),
            sa.ForeignKey("orders.client_order_id"),
            nullable=False,
        ),
        sa.Column("broker_order_id", sa.String(32), nullable=True),
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("tradingsymbol", sa.String(64), nullable=False),
        sa.Column("instrument_token", sa.BigInteger, nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("product", sa.String(8), nullable=False),
        sa.Column("charges", sa.Float, nullable=False),
        sa.Column("ts", TS, nullable=False),
    )
    op.create_index("ix_fills_client_order_id", "fills", ["client_order_id"])
    op.create_index("ix_fills_date_strategy", "fills", ["trading_date", "strategy_id"])

    op.create_table(
        "candles",
        sa.Column("instrument_token", sa.BigInteger, nullable=False),
        sa.Column("interval", sa.String(16), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
        sa.Column("oi", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("instrument_token", "interval", "ts", name="pk_candles"),
    )
    # Unique (token, interval, ts) is the primary key above; it includes the time
    # column, as TimescaleDB requires for unique indexes on hypertables.
    op.execute(
        "SELECT create_hypertable('candles', by_range('ts', INTERVAL '7 days'), "
        "if_not_exists => TRUE)"
    )

    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("metrics", JSONB, nullable=True),
        sa.Column("equity_curve", JSONB, nullable=True),
        sa.Column("missing_data", JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )

    op.create_table(
        "daily_pnl",
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("gross_pnl", sa.Float, nullable=False),
        sa.Column("charges", sa.Float, nullable=False),
        sa.Column("net_pnl", sa.Float, nullable=False),
        sa.Column("trades", sa.Integer, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("trading_date", "mode", "strategy_id", name="pk_daily_pnl"),
    )

    op.create_table(
        "leg_groups",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("legs", JSONB, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_leg_groups_trading_date", "leg_groups", ["trading_date"])


def downgrade() -> None:
    for table in (
        "leg_groups",
        "daily_pnl",
        "backtest_runs",
        "candles",
        "fills",
        "order_events",
        "orders",
    ):
        op.drop_table(table)
