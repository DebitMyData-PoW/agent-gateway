"""Computed prices with their inputs (the DMD mining-cost price), append-only.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("asset_symbol", sa.Text(), nullable=False),
        sa.Column("network", sa.Text(), nullable=False),
        sa.Column("usd_per_unit", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("usd_per_unit > 0", name=op.f("ck_price_records_price_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_records")),
    )
    op.create_index(
        "ix_price_records_latest",
        "price_records",
        ["source", "asset_symbol", "observed_at"],
        unique=False,
    )
    op.execute(
        "CREATE TRIGGER price_records_no_update_delete BEFORE UPDATE OR DELETE "
        "ON price_records FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER price_records_no_truncate BEFORE TRUNCATE "
        "ON price_records FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()"
    )


def downgrade() -> None:
    op.drop_index("ix_price_records_latest", table_name="price_records")
    op.drop_table("price_records")
