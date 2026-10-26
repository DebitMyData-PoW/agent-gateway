"""Monitoring: the payment worker's heartbeat and other monitor values, and alert states.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitor_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_monitor_state")),
    )
    op.create_table(
        "alert_states",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notifications", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "severity IN ('critical', 'warning')", name=op.f("ck_alert_states_severity")
        ),
        sa.CheckConstraint("kind IN ('condition', 'event')", name=op.f("ck_alert_states_kind")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_alert_states")),
    )


def downgrade() -> None:
    op.drop_table("alert_states")
    op.drop_table("monitor_state")
