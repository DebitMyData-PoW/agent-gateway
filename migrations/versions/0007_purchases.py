"""Purchases other than DID registrations (inference donations), and payments that belong to
either a registration or a purchase.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "status IN ('awaiting_payment', 'paid', 'completed', 'failed')"


def upgrade() -> None:
    op.create_table(
        "purchases",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("commercial_price_currency", sa.Text(), nullable=False),
        sa.Column("commercial_price_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("donor_name", sa.Text(), nullable=True),
        sa.Column("donor_did", sa.Text(), nullable=True),
        sa.Column("terms_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(STATUSES, name=op.f("ck_purchases_status")),
        sa.CheckConstraint("commercial_price_amount > 0", name=op.f("ck_purchases_price_positive")),
        sa.CheckConstraint(
            "status <> 'completed' OR receipt IS NOT NULL",
            name=op.f("ck_purchases_receipt_present"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchases")),
    )
    op.add_column("payment_intents", sa.Column("purchase_id", sa.Text(), nullable=True))
    op.create_index(
        op.f("ix_payment_intents_purchase_id"), "payment_intents", ["purchase_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_payment_intents_purchase_id_purchases"),
        "payment_intents",
        "purchases",
        ["purchase_id"],
        ["id"],
    )
    op.alter_column("payment_intents", "registration_intent_id", nullable=True)
    op.create_check_constraint(
        op.f("ck_payment_intents_one_owner"),
        "payment_intents",
        "num_nonnulls(registration_intent_id, purchase_id) = 1",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_payment_intents_one_owner"), "payment_intents", type_="check")
    # Payment records are append-only and never deleted: when purchases were paid, their
    # payments stay, so registration_intent_id stays optional.
    paid_purchases = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM payment_intents WHERE purchase_id IS NOT NULL)")
    )
    if not paid_purchases.scalar():
        op.alter_column("payment_intents", "registration_intent_id", nullable=False)
    op.drop_constraint(
        op.f("fk_payment_intents_purchase_id_purchases"), "payment_intents", type_="foreignkey"
    )
    op.drop_index(op.f("ix_payment_intents_purchase_id"), table_name="payment_intents")
    op.drop_column("payment_intents", "purchase_id")
    op.drop_table("purchases")
