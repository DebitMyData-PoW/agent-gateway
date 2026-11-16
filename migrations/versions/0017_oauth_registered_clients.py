"""OAuth apps that registered themselves (RFC 7591), for the apps that cannot use a Client ID
Metadata Document: Google's (Gemini), which return people to Google's own addresses only.
Each has a client secret, kept as its SHA-256 only.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "oauth_registered_clients"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("redirect_uris", JSONB(), nullable=False),
        sa.Column("auth_method", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "auth_method IN ('client_secret_basic', 'client_secret_post')",
            name=op.f(f"ck_{TABLE}_auth_method"),
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f(f"pk_{TABLE}")),
    )
    op.create_index(op.f(f"ix_{TABLE}_created_at"), TABLE, ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{TABLE}_created_at"), table_name=TABLE)
    op.drop_table(TABLE)
