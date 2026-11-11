"""Card payments: a payment intent's recipient can be a payment page made for it alone
(Stripe Checkout), with the provider's ID for that page as its payment reference.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = "recipient_kind IN ('per_intent_address', 'shared_with_reference')"
NEW_KINDS = (
    "recipient_kind IN ('per_intent_address', 'shared_with_reference', 'hosted_payment_page')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_payment_intents_recipient_kind"), "payment_intents", type_="check")
    op.create_check_constraint(
        op.f("ck_payment_intents_recipient_kind"), "payment_intents", NEW_KINDS
    )


def downgrade() -> None:
    # Payment records are append-only and never deleted: when card payments were made, their
    # payments stay, so the constraint keeps allowing them.
    card_payments = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM payment_intents "
                "WHERE recipient_kind = 'hosted_payment_page')"
            )
        )
        .scalar()
    )
    if card_payments:
        return
    op.drop_constraint(op.f("ck_payment_intents_recipient_kind"), "payment_intents", type_="check")
    op.create_check_constraint(
        op.f("ck_payment_intents_recipient_kind"), "payment_intents", OLD_KINDS
    )
