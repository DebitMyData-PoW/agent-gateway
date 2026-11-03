"""Products bought for a DID (BottyBotty protection): the purchase names the DID it is for and
until when it covers it; an entitlement can end.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "purchases",
        sa.Column(
            "subject_identity_id",
            sa.Text(),
            sa.ForeignKey("identities.id", name="fk_purchases_subject_identity_id_identities"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_purchases_subject_identity_id", "purchases", ["subject_identity_id"], unique=False
    )
    op.add_column("purchases", sa.Column("subject_did", sa.Text(), nullable=True))
    op.add_column(
        "purchases", sa.Column("covers_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "entitlements", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("entitlements", "expires_at")
    op.drop_column("purchases", "covers_until")
    op.drop_column("purchases", "subject_did")
    op.drop_index("ix_purchases_subject_identity_id", table_name="purchases")
    op.drop_column("purchases", "subject_identity_id")
