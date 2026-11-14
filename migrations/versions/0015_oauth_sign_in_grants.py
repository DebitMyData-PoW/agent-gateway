"""OAuth phase 2 in production: each browser sign-in keeps the token signer's grant (made once
the signer checked the owner's signature), and its code and refresh tokens carry it.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("oauth_sign_ins", "oauth_codes", "oauth_refresh_tokens")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("sign_in_grant", sa.Text(), nullable=True))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_column(table, "sign_in_grant")
