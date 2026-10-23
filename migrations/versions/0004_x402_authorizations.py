"""x402 payment authorizations received for payment intents.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE = "status IN ('received', 'submitted', 'settled')"


def upgrade() -> None:
    op.create_table(
        "x402_authorizations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("payment_intent_id", sa.Text(), nullable=False),
        sa.Column("network", sa.Text(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("pay_to", sa.Text(), nullable=False),
        sa.Column("payer", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("valid_after", sa.BigInteger(), nullable=False),
        sa.Column("valid_before", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requirements", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("search_from_block", sa.BigInteger(), nullable=True),
        sa.Column("settle_transaction", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('received', 'verify_rejected', 'submitted', 'settled', 'settle_rejected')",
            name=op.f("ck_x402_authorizations_status"),
        ),
        sa.CheckConstraint("value > 0", name=op.f("ck_x402_authorizations_value_positive")),
        sa.CheckConstraint(
            "status <> 'settled' OR settle_transaction IS NOT NULL",
            name=op.f("ck_x402_authorizations_settled_has_transaction"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_intent_id"],
            ["payment_intents.id"],
            name=op.f("fk_x402_authorizations_payment_intent_id_payment_intents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_x402_authorizations")),
        sa.UniqueConstraint(
            "network",
            "asset",
            "payer",
            "nonce",
            name=op.f("uq_x402_authorizations_network_asset_payer_nonce"),
        ),
    )
    op.create_index(
        op.f("ix_x402_authorizations_payment_intent_id"),
        "x402_authorizations",
        ["payment_intent_id"],
        unique=False,
    )
    op.create_index(
        "uq_x402_authorizations_live_per_intent",
        "x402_authorizations",
        ["payment_intent_id"],
        unique=True,
        postgresql_where=sa.text(LIVE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_x402_authorizations_live_per_intent",
        table_name="x402_authorizations",
        postgresql_where=sa.text(LIVE),
    )
    op.drop_index(
        op.f("ix_x402_authorizations_payment_intent_id"), table_name="x402_authorizations"
    )
    op.drop_table("x402_authorizations")
