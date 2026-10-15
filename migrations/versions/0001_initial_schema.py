"""Initial schema: intents, payments, identities, proofs, credentials, entitlements, audit.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept here rather than imported so this migration never changes when models do.
APPEND_ONLY_TABLES = ("audit_events", "payment_events", "credited_outputs")

FORBID_MUTATION_FUNCTION = """
CREATE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only (% refused)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_type IN ('system', 'worker', 'agent', 'operator')",
            name=op.f("ck_audit_events_actor_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"], unique=False)
    op.create_index(
        "ix_audit_events_subject", "audit_events", ["subject_type", "subject_id"], unique=False
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("scope", "key", name=op.f("pk_idempotency_keys")),
    )
    op.create_table(
        "policy_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('terms', 'privacy', 'payment', 'refund', 'did', 'marketplace', 'security')",
            name=op.f("ck_policy_versions_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_versions")),
        sa.UniqueConstraint("kind", "version", name=op.f("uq_policy_versions_kind_version")),
    )
    op.create_table(
        "registration_intents",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("service_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("participant_type", sa.Text(), nullable=False),
        sa.Column("controller_public_key", sa.Text(), nullable=True),
        sa.Column("controller_wallet_address", sa.Text(), nullable=True),
        sa.Column("controller_network", sa.Text(), nullable=True),
        sa.Column("commercial_price_currency", sa.Text(), nullable=False),
        sa.Column("commercial_price_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("terms_version", sa.Text(), nullable=False),
        sa.Column(
            "agent_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_payment', 'paid', 'under_review', 'issued', 'expired', 'rejected', 'cancelled')",
            name=op.f("ck_registration_intents_status"),
        ),
        sa.CheckConstraint(
            "commercial_price_amount > 0", name=op.f("ck_registration_intents_price_positive")
        ),
        sa.CheckConstraint(
            "controller_public_key IS NOT NULL OR controller_wallet_address IS NOT NULL",
            name=op.f("ck_registration_intents_controller_present"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_registration_intents")),
    )
    op.create_table(
        "identities",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("public_identifier", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("participant_type", sa.Text(), nullable=False),
        sa.Column("controller_public_key", sa.Text(), nullable=True),
        sa.Column("controller_wallet_address", sa.Text(), nullable=True),
        sa.Column("controller_network", sa.Text(), nullable=True),
        sa.Column("registration_intent_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')", name=op.f("ck_identities_status")
        ),
        sa.CheckConstraint(
            "controller_public_key IS NOT NULL OR controller_wallet_address IS NOT NULL",
            name=op.f("ck_identities_controller_present"),
        ),
        sa.ForeignKeyConstraint(
            ["registration_intent_id"],
            ["registration_intents.id"],
            name=op.f("fk_identities_registration_intent_id_registration_intents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identities")),
        sa.UniqueConstraint("public_identifier", name=op.f("uq_identities_public_identifier")),
        sa.UniqueConstraint(
            "registration_intent_id", name=op.f("uq_identities_registration_intent_id")
        ),
    )
    op.create_table(
        "payment_intents",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("registration_intent_id", sa.Text(), nullable=False),
        sa.Column("rail_id", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("asset_symbol", sa.Text(), nullable=False),
        sa.Column("asset_canonical_id", sa.Text(), nullable=False),
        sa.Column("asset_decimals", sa.SmallInteger(), nullable=False),
        sa.Column("network", sa.Text(), nullable=False),
        sa.Column("amount_base_units", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("commercial_price_currency", sa.Text(), nullable=False),
        sa.Column("commercial_price_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("price_source", sa.Text(), nullable=True),
        sa.Column("price_usd_per_unit", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("price_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("recipient_kind", sa.Text(), nullable=False),
        sa.Column("payment_reference", sa.Text(), nullable=True),
        sa.Column("required_confirmations", sa.Integer(), nullable=True),
        sa.Column("finality_policy", sa.Text(), nullable=True),
        sa.Column("quote_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "recipient_kind <> 'shared_with_reference' OR payment_reference IS NOT NULL",
            name=op.f("ck_payment_intents_shared_recipient_needs_reference"),
        ),
        sa.CheckConstraint(
            "recipient_kind IN ('per_intent_address', 'shared_with_reference')",
            name=op.f("ck_payment_intents_recipient_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('created', 'awaiting_payment', 'detected', 'confirming', 'verified', 'settled', 'failed', 'expired', 'underpaid', 'overpaid', 'reorged', 'reversed', 'refunded', 'disputed')",
            name=op.f("ck_payment_intents_status"),
        ),
        sa.CheckConstraint(
            "amount_base_units > 0", name=op.f("ck_payment_intents_amount_positive")
        ),
        sa.CheckConstraint(
            "asset_decimals BETWEEN 0 AND 36", name=op.f("ck_payment_intents_asset_decimals")
        ),
        sa.CheckConstraint(
            "num_nulls(price_source, price_usd_per_unit, price_observed_at) IN (0, 3)",
            name=op.f("ck_payment_intents_price_inputs_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["registration_intent_id"],
            ["registration_intents.id"],
            name=op.f("fk_payment_intents_registration_intent_id_registration_intents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_intents")),
    )
    op.create_index(
        op.f("ix_payment_intents_registration_intent_id"),
        "payment_intents",
        ["registration_intent_id"],
        unique=False,
    )
    op.create_index("ix_payment_intents_status", "payment_intents", ["status"], unique=False)
    op.create_index(
        "uq_payment_intents_payment_reference",
        "payment_intents",
        ["network", "payment_reference"],
        unique=True,
        postgresql_where=sa.text("payment_reference IS NOT NULL"),
    )
    op.create_index(
        "uq_payment_intents_per_intent_recipient",
        "payment_intents",
        ["network", "recipient"],
        unique=True,
        postgresql_where=sa.text("recipient_kind = 'per_intent_address'"),
    )
    op.create_table(
        "controller_challenges",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("registration_intent_id", sa.Text(), nullable=True),
        sa.Column("identity_id", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("controller_type", sa.Text(), nullable=False),
        sa.Column("controller_value", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('registration', 'rotation', 'recovery')",
            name=op.f("ck_controller_challenges_purpose"),
        ),
        sa.CheckConstraint(
            "(registration_intent_id IS NOT NULL) <> (identity_id IS NOT NULL)",
            name=op.f("ck_controller_challenges_exactly_one_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["identities.id"],
            name=op.f("fk_controller_challenges_identity_id_identities"),
        ),
        sa.ForeignKeyConstraint(
            ["registration_intent_id"],
            ["registration_intents.id"],
            name=op.f("fk_controller_challenges_registration_intent_id_registration_intents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_controller_challenges")),
        sa.UniqueConstraint("nonce", name=op.f("uq_controller_challenges_nonce")),
    )
    op.create_index(
        op.f("ix_controller_challenges_identity_id"),
        "controller_challenges",
        ["identity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_controller_challenges_registration_intent_id"),
        "controller_challenges",
        ["registration_intent_id"],
        unique=False,
    )
    op.create_table(
        "credentials",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("identity_id", sa.Text(), nullable=False),
        sa.Column("registration_intent_id", sa.Text(), nullable=False),
        sa.Column("payment_intent_id", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("issuer_key_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["identity_id"], ["identities.id"], name=op.f("fk_credentials_identity_id_identities")
        ),
        sa.ForeignKeyConstraint(
            ["payment_intent_id"],
            ["payment_intents.id"],
            name=op.f("fk_credentials_payment_intent_id_payment_intents"),
        ),
        sa.ForeignKeyConstraint(
            ["registration_intent_id"],
            ["registration_intents.id"],
            name=op.f("fk_credentials_registration_intent_id_registration_intents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credentials")),
    )
    op.create_index(
        op.f("ix_credentials_identity_id"), "credentials", ["identity_id"], unique=False
    )
    op.create_table(
        "credited_outputs",
        sa.Column("network", sa.Text(), nullable=False),
        sa.Column("transaction_reference", sa.Text(), nullable=False),
        sa.Column("output_index", sa.Integer(), nullable=False),
        sa.Column("payment_intent_id", sa.Text(), nullable=False),
        sa.Column("amount_base_units", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column(
            "credited_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_base_units > 0", name=op.f("ck_credited_outputs_amount_positive")
        ),
        sa.ForeignKeyConstraint(
            ["payment_intent_id"],
            ["payment_intents.id"],
            name=op.f("fk_credited_outputs_payment_intent_id_payment_intents"),
        ),
        sa.PrimaryKeyConstraint(
            "network", "transaction_reference", "output_index", name=op.f("pk_credited_outputs")
        ),
    )
    op.create_index(
        op.f("ix_credited_outputs_payment_intent_id"),
        "credited_outputs",
        ["payment_intent_id"],
        unique=False,
    )
    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("payment_intent_id", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("transaction_reference", sa.Text(), nullable=True),
        sa.Column("observed_amount_base_units", sa.Numeric(precision=78, scale=0), nullable=True),
        sa.Column("confirmations", sa.Integer(), nullable=True),
        sa.Column("block_hash", sa.Text(), nullable=True),
        sa.Column("block_height", sa.BigInteger(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN ('created', 'awaiting_payment', 'detected', 'confirming', 'verified', 'settled', 'failed', 'expired', 'underpaid', 'overpaid', 'reorged', 'reversed', 'refunded', 'disputed')",
            name=op.f("ck_payment_events_from_status"),
        ),
        sa.CheckConstraint(
            "to_status IN ('created', 'awaiting_payment', 'detected', 'confirming', 'verified', 'settled', 'failed', 'expired', 'underpaid', 'overpaid', 'reorged', 'reversed', 'refunded', 'disputed')",
            name=op.f("ck_payment_events_to_status"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_intent_id"],
            ["payment_intents.id"],
            name=op.f("fk_payment_events_payment_intent_id_payment_intents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_events")),
    )
    op.create_index(
        op.f("ix_payment_events_payment_intent_id"),
        "payment_events",
        ["payment_intent_id"],
        unique=False,
    )
    op.create_table(
        "controller_proofs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("challenge_id", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('verified', 'rejected')", name=op.f("ck_controller_proofs_status")
        ),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["controller_challenges.id"],
            name=op.f("fk_controller_proofs_challenge_id_controller_challenges"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_controller_proofs")),
        sa.UniqueConstraint("challenge_id", name=op.f("uq_controller_proofs_challenge_id")),
    )
    op.create_table(
        "entitlements",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("entitlement", sa.Text(), nullable=False),
        sa.Column("source_credential_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')", name=op.f("ck_entitlements_status")
        ),
        sa.ForeignKeyConstraint(
            ["source_credential_id"],
            ["credentials.id"],
            name=op.f("fk_entitlements_source_credential_id_credentials"),
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["identities.id"], name=op.f("fk_entitlements_subject_id_identities")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entitlements")),
        sa.UniqueConstraint(
            "subject_id", "entitlement", name=op.f("uq_entitlements_subject_id_entitlement")
        ),
    )

    # Append-only tables (GW-05): reject UPDATE, DELETE and TRUNCATE at the database level.
    op.execute(FORBID_MUTATION_FUNCTION)
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update_delete ON {table}")
    op.execute("DROP FUNCTION IF EXISTS forbid_mutation()")

    op.drop_table("entitlements")
    op.drop_table("controller_proofs")
    op.drop_index(op.f("ix_payment_events_payment_intent_id"), table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_index(op.f("ix_credited_outputs_payment_intent_id"), table_name="credited_outputs")
    op.drop_table("credited_outputs")
    op.drop_index(op.f("ix_credentials_identity_id"), table_name="credentials")
    op.drop_table("credentials")
    op.drop_index(
        op.f("ix_controller_challenges_registration_intent_id"), table_name="controller_challenges"
    )
    op.drop_index(op.f("ix_controller_challenges_identity_id"), table_name="controller_challenges")
    op.drop_table("controller_challenges")
    op.drop_index(
        "uq_payment_intents_per_intent_recipient",
        table_name="payment_intents",
        postgresql_where=sa.text("recipient_kind = 'per_intent_address'"),
    )
    op.drop_index(
        "uq_payment_intents_payment_reference",
        table_name="payment_intents",
        postgresql_where=sa.text("payment_reference IS NOT NULL"),
    )
    op.drop_index("ix_payment_intents_status", table_name="payment_intents")
    op.drop_index(op.f("ix_payment_intents_registration_intent_id"), table_name="payment_intents")
    op.drop_table("payment_intents")
    op.drop_table("identities")
    op.drop_table("registration_intents")
    op.drop_table("policy_versions")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_audit_events_subject", table_name="audit_events")
    op.drop_index("ix_audit_events_occurred_at", table_name="audit_events")
    op.drop_table("audit_events")
