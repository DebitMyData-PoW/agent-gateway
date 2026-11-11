"""The DID-LLM directory: owners list their DID-LLM with a public profile, signed with its
controller key or wallet (a controller challenge for the purpose ``directory_listing``,
carrying the proposed change).

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PURPOSES = "purpose IN ('registration', 'rotation', 'recovery')"
NEW_PURPOSES = "purpose IN ('registration', 'rotation', 'recovery', 'directory_listing')"


def upgrade() -> None:
    op.add_column(
        "controller_challenges",
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_constraint(
        op.f("ck_controller_challenges_purpose"), "controller_challenges", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_controller_challenges_purpose"), "controller_challenges", NEW_PURPOSES
    )
    op.create_check_constraint(
        op.f("ck_controller_challenges_proposal_present"),
        "controller_challenges",
        "(purpose = 'directory_listing') = (proposal IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_controller_challenges_listing_names_identity"),
        "controller_challenges",
        "purpose <> 'directory_listing' OR identity_id IS NOT NULL",
    )
    op.create_table(
        "directory_listings",
        sa.Column("identity_id", sa.Text(), nullable=False),
        sa.Column("listed", sa.Boolean(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("specialty", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_name", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("created_on", sa.Date(), nullable=True),
        sa.Column("terms_version", sa.Text(), nullable=False),
        sa.Column("challenge_id", sa.Text(), nullable=False),
        sa.Column("proof_id", sa.Text(), nullable=False),
        sa.Column("listed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hidden_reason", sa.Text(), nullable=True),
        sa.Column("hidden_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "entity_type IS NULL OR entity_type IN ('avatar', 'robot', 'sentient')",
            name=op.f("ck_directory_listings_entity_type"),
        ),
        sa.CheckConstraint(
            "NOT listed OR (entity_type IS NOT NULL AND specialty IS NOT NULL "
            "AND description IS NOT NULL AND listed_at IS NOT NULL)",
            name=op.f("ck_directory_listings_listed_has_profile"),
        ),
        sa.CheckConstraint(
            "(hidden_at IS NULL) = (hidden_reason IS NULL) AND "
            "(hidden_at IS NULL) = (hidden_by IS NULL)",
            name=op.f("ck_directory_listings_hidden_all_or_nothing"),
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["identities.id"],
            name=op.f("fk_directory_listings_identity_id_identities"),
        ),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["controller_challenges.id"],
            name=op.f("fk_directory_listings_challenge_id_controller_challenges"),
        ),
        sa.ForeignKeyConstraint(
            ["proof_id"],
            ["controller_proofs.id"],
            name=op.f("fk_directory_listings_proof_id_controller_proofs"),
        ),
        sa.PrimaryKeyConstraint("identity_id", name=op.f("pk_directory_listings")),
    )
    op.create_index(
        "ix_directory_listings_shown", "directory_listings", ["listed_at", "identity_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_directory_listings_shown", table_name="directory_listings")
    op.drop_table("directory_listings")
    listing_changes = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM controller_challenges "
                "WHERE purpose = 'directory_listing')"
            )
        )
        .scalar()
    )
    op.drop_constraint(
        op.f("ck_controller_challenges_listing_names_identity"),
        "controller_challenges",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_controller_challenges_proposal_present"), "controller_challenges", type_="check"
    )
    if listing_changes:
        # Signed listing changes have proofs, which are append-only and never deleted: their
        # challenges stay, with their purpose and proposal.
        return
    op.drop_constraint(
        op.f("ck_controller_challenges_purpose"), "controller_challenges", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_controller_challenges_purpose"), "controller_challenges", OLD_PURPOSES
    )
    op.drop_column("controller_challenges", "proposal")
