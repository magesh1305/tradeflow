"""instrument master history (for backtests across expired contracts)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("tradingsymbol", sa.String(64), nullable=False),
        sa.Column("instrument_token", sa.BigInteger, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("instrument_type", sa.String(8), nullable=False),
        sa.Column("lot_size", sa.Integer, nullable=False),
        sa.Column("tick_size", sa.Float, nullable=False),
        sa.Column("expiry", sa.Date, nullable=True),
        sa.Column("strike", sa.Float, nullable=True),
        sa.Column("first_seen", sa.Date, nullable=False),
        sa.Column("last_seen", sa.Date, nullable=False),
        sa.PrimaryKeyConstraint("exchange", "tradingsymbol", name="pk_instruments"),
    )
    op.create_index("ix_instruments_instrument_token", "instruments", ["instrument_token"])
    op.create_index("ix_instruments_name", "instruments", ["name"])


def downgrade() -> None:
    op.drop_table("instruments")
