"""daily trading plans (Settings tab) and their audit trail

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "daily_plans",
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("submitted_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("trading_date", "mode", name="pk_daily_plans"),
    )
    op.create_table(
        "plan_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("trading_date", sa.Date, nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("changes", JSONB, nullable=False),
        sa.Column("loosened", JSONB, nullable=False),
        sa.Column("plan", JSONB, nullable=False),
    )
    op.create_index("ix_plan_events_trading_date", "plan_events", ["trading_date"])


def downgrade() -> None:
    op.drop_table("plan_events")
    op.drop_table("daily_plans")
