"""Payer-submitted transaction references (hints only), append-only.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_references",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("payment_intent_id", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["payment_intent_id"],
            ["payment_intents.id"],
            name=op.f("fk_payment_references_payment_intent_id_payment_intents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_references")),
        sa.UniqueConstraint(
            "payment_intent_id",
            "reference",
            name=op.f("uq_payment_references_payment_intent_id_reference"),
        ),
    )
    op.create_index(
        op.f("ix_payment_references_payment_intent_id"),
        "payment_references",
        ["payment_intent_id"],
        unique=False,
    )
    op.execute(
        "CREATE TRIGGER payment_references_no_update_delete BEFORE UPDATE OR DELETE "
        "ON payment_references FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER payment_references_no_truncate BEFORE TRUNCATE "
        "ON payment_references FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_references_payment_intent_id"), table_name="payment_references")
    op.drop_table("payment_references")
