"""Index for counting a controller key's open quotes (quote limit per controller key).

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_registration_intents_open_by_controller",
        "registration_intents",
        ["controller_public_key", "expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'awaiting_payment'"),
    )


def downgrade() -> None:
    op.drop_index("ix_registration_intents_open_by_controller", table_name="registration_intents")
