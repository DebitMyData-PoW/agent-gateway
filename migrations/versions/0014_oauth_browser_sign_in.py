"""OAuth phase 2: signing in to an app with a DID-LLM in the browser (sign-ins, authorization
codes, refresh tokens).

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "oauth_sign_ins",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("identity_id", sa.Text(), nullable=True),
        sa.Column("controller_type", sa.Text(), nullable=True),
        sa.Column("controller_value", sa.Text(), nullable=True),
        sa.Column("nonce", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('open', 'approved', 'finished', 'denied')",
            name=op.f("ck_oauth_sign_ins_status"),
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["identities.id"],
            name=op.f("fk_oauth_sign_ins_identity_id_identities"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_sign_ins")),
        sa.UniqueConstraint("nonce", name=op.f("uq_oauth_sign_ins_nonce")),
    )
    op.create_index(
        op.f("ix_oauth_sign_ins_identity_id"), "oauth_sign_ins", ["identity_id"], unique=False
    )
    op.create_index(
        op.f("ix_oauth_sign_ins_expires_at"), "oauth_sign_ins", ["expires_at"], unique=False
    )

    op.create_table(
        "oauth_codes",
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("sign_in_id", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("identity_id", sa.Text(), nullable=False),
        sa.Column("did", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("family_id", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["sign_in_id"],
            ["oauth_sign_ins.id"],
            name=op.f("fk_oauth_codes_sign_in_id_oauth_sign_ins"),
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"], ["identities.id"], name=op.f("fk_oauth_codes_identity_id_identities")
        ),
        sa.PrimaryKeyConstraint("code_hash", name=op.f("pk_oauth_codes")),
        sa.UniqueConstraint("sign_in_id", name=op.f("uq_oauth_codes_sign_in_id")),
    )
    op.create_index(op.f("ix_oauth_codes_expires_at"), "oauth_codes", ["expires_at"], unique=False)

    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("family_id", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("identity_id", sa.Text(), nullable=False),
        sa.Column("did", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["identities.id"],
            name=op.f("fk_oauth_refresh_tokens_identity_id_identities"),
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_oauth_refresh_tokens")),
    )
    for column in ("family_id", "identity_id", "expires_at"):
        op.create_index(
            op.f(f"ix_oauth_refresh_tokens_{column}"),
            "oauth_refresh_tokens",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in ("family_id", "identity_id", "expires_at"):
        op.drop_index(op.f(f"ix_oauth_refresh_tokens_{column}"), table_name="oauth_refresh_tokens")
    op.drop_table("oauth_refresh_tokens")
    op.drop_index(op.f("ix_oauth_codes_expires_at"), table_name="oauth_codes")
    op.drop_table("oauth_codes")
    op.drop_index(op.f("ix_oauth_sign_ins_expires_at"), table_name="oauth_sign_ins")
    op.drop_index(op.f("ix_oauth_sign_ins_identity_id"), table_name="oauth_sign_ins")
    op.drop_table("oauth_sign_ins")
