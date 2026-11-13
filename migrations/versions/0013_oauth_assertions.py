"""OAuth phase 1: the agents' signed requests for access tokens, each used once.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_assertions",
        sa.Column("did", sa.Text(), nullable=False),
        sa.Column("jti", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("did", "jti", name=op.f("pk_oauth_assertions")),
    )
    op.create_index(
        op.f("ix_oauth_assertions_expires_at"), "oauth_assertions", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_assertions_expires_at"), table_name="oauth_assertions")
    op.drop_table("oauth_assertions")
