"""Owner changes and owner revocation (ID-12): an issued DID-LLM's owner key and/or wallet is
replaced once one of its current owners approved it and each new owner accepted it, by signing
(``owner_changes``, with its messages: controller challenges for the purpose ``rotation``), and
an owner may revoke the DID-LLM for good (the purpose ``revocation``). Sign-ins from before an
owner change no longer count (``identities.controller_changed_at``).

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PURPOSES = "purpose IN ('registration', 'rotation', 'recovery', 'directory_listing')"
NEW_PURPOSES = (
    "purpose IN ('registration', 'rotation', 'recovery', 'directory_listing', 'revocation')"
)
OLD_NAMES_IDENTITY = "purpose <> 'directory_listing' OR identity_id IS NOT NULL"
NEW_NAMES_IDENTITY = (
    "purpose NOT IN ('directory_listing', 'rotation', 'revocation') OR identity_id IS NOT NULL"
)
CHALLENGES = "controller_challenges"


def _replace_check(name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{CHALLENGES}_{name}"), CHALLENGES, type_="check")
    op.create_check_constraint(op.f(f"ck_{CHALLENGES}_{name}"), CHALLENGES, condition)


def upgrade() -> None:
    op.add_column(
        "identities",
        sa.Column("controller_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "owner_changes",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("identity_id", sa.Text(), nullable=False),
        sa.Column("previous_public_key", sa.Text(), nullable=True),
        sa.Column("previous_wallet_address", sa.Text(), nullable=True),
        sa.Column("previous_network", sa.Text(), nullable=True),
        sa.Column("new_public_key", sa.Text(), nullable=True),
        sa.Column("new_wallet_address", sa.Text(), nullable=True),
        sa.Column("new_network", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('open', 'applied', 'cancelled')", name=op.f("ck_owner_changes_status")
        ),
        sa.CheckConstraint(
            "new_public_key IS NOT NULL OR new_wallet_address IS NOT NULL",
            name=op.f("ck_owner_changes_new_owner"),
        ),
        sa.CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL)",
            name=op.f("ck_owner_changes_applied_at_set"),
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"], ["identities.id"], name=op.f("fk_owner_changes_identity_id_identities")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_owner_changes")),
    )
    op.create_index(op.f("ix_owner_changes_identity_id"), "owner_changes", ["identity_id"])
    op.add_column(CHALLENGES, sa.Column("owner_change_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{CHALLENGES}_owner_change_id_owner_changes"),
        CHALLENGES,
        "owner_changes",
        ["owner_change_id"],
        ["id"],
    )
    op.create_index(op.f(f"ix_{CHALLENGES}_owner_change_id"), CHALLENGES, ["owner_change_id"])
    _replace_check("purpose", NEW_PURPOSES)
    _replace_check("listing_names_identity", NEW_NAMES_IDENTITY)
    op.create_check_constraint(
        op.f(f"ck_{CHALLENGES}_rotation_has_change"),
        CHALLENGES,
        "(purpose = 'rotation') = (owner_change_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_{CHALLENGES}_rotation_has_change"), CHALLENGES, type_="check")
    op.drop_index(op.f(f"ix_{CHALLENGES}_owner_change_id"), table_name=CHALLENGES)
    op.drop_constraint(
        op.f(f"fk_{CHALLENGES}_owner_change_id_owner_changes"), CHALLENGES, type_="foreignkey"
    )
    signed = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM controller_challenges "
                "WHERE purpose IN ('rotation', 'revocation'))"
            )
        )
        .scalar()
    )
    if not signed:
        _replace_check("listing_names_identity", OLD_NAMES_IDENTITY)
        _replace_check("purpose", OLD_PURPOSES)
        op.drop_column(CHALLENGES, "owner_change_id")
    # Otherwise owners' signed messages have proofs, which are append-only and never deleted:
    # their challenges stay, with their purpose and change ID.
    op.drop_index(op.f("ix_owner_changes_identity_id"), table_name="owner_changes")
    op.drop_table("owner_changes")
    op.drop_column("identities", "controller_changed_at")
