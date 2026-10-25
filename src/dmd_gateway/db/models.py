"""Persistent schema (GW-04, GW-05).

Conventions:
  * Token amounts are integer base units in NUMERIC(78, 0) (wide enough for 18-decimal
    ERC-20 tokens); USD amounts are NUMERIC(12, 2). Never floats.
  * Status columns are constrained to their allowed values in the database itself.
  * ``audit_events``, ``payment_events``, ``credited_outputs``, ``controller_proofs`` and
    ``payment_references`` are append-only: the migrations install triggers that reject
    UPDATE, DELETE and TRUNCATE.
  * ``credited_outputs`` has a primary key on (network, transaction, output) so a single
    payment output can never be credited to two intents (DMD-11).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dmd_gateway.domain.payment_status import PaymentStatus

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TokenUnits = Numeric(78, 0)
UsdAmount = Numeric(12, 2)
Timestamp = DateTime(timezone=True)

PAYMENT_STATUSES = tuple(status.value for status in PaymentStatus)
REGISTRATION_STATUSES = (
    "awaiting_payment",
    "paid",
    "under_review",
    "issued",
    "expired",
    "rejected",
    "cancelled",
)
IDENTITY_STATUSES = ("active", "suspended", "revoked")
PROOF_STATUSES = ("verified", "rejected")
CHALLENGE_PURPOSES = ("registration", "rotation", "recovery")
RECIPIENT_KINDS = ("per_intent_address", "shared_with_reference")
ACTOR_TYPES = ("system", "worker", "agent", "operator")
X402_AUTHORIZATION_STATUSES = (
    "received",  # stored, not yet checked by the facilitator
    "verify_rejected",  # refused before settlement: no funds can have moved
    "submitted",  # sent for settlement; the outcome is checked on chain
    "settled",  # the facilitator reported a transaction (still checked on chain)
    "settle_rejected",  # the facilitator refused to settle (still watched on chain)
)
# At most one authorization per payment intent is in progress or settled at a time.
X402_LIVE_STATUSES = ("received", "submitted", "settled")
POLICY_KINDS = ("terms", "privacy", "payment", "refund", "did", "marketplace", "security")
CONTROLLER_PRESENT = "controller_public_key IS NOT NULL OR controller_wallet_address IS NOT NULL"


def one_of(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def created_at() -> Mapped[datetime]:
    return mapped_column(Timestamp, nullable=False, server_default=func.now())


def updated_at() -> Mapped[datetime]:
    return mapped_column(Timestamp, nullable=False, server_default=func.now())


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("kind", "version"),
        CheckConstraint(one_of("kind", POLICY_KINDS), name="kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(Text)
    effective_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    created_at: Mapped[datetime] = created_at()


class RegistrationIntent(Base):
    __tablename__ = "registration_intents"
    __table_args__ = (
        CheckConstraint(one_of("status", REGISTRATION_STATUSES), name="status"),
        CheckConstraint("commercial_price_amount > 0", name="price_positive"),
        CheckConstraint(CONTROLLER_PRESENT, name="controller_present"),
        # Counting a controller's open quotes (quote limit per controller key).
        Index(
            "ix_registration_intents_open_by_controller",
            "controller_public_key",
            "expires_at",
            postgresql_where=text("status = 'awaiting_payment'"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    service_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    participant_type: Mapped[str] = mapped_column(Text, nullable=False)
    # As in the directive's quote request: a controller public key and/or a wallet address.
    controller_public_key: Mapped[str | None] = mapped_column(Text)
    controller_wallet_address: Mapped[str | None] = mapped_column(Text)
    controller_network: Mapped[str | None] = mapped_column(Text)
    commercial_price_currency: Mapped[str] = mapped_column(Text, nullable=False)
    commercial_price_amount: Mapped[Decimal] = mapped_column(UsdAmount, nullable=False)
    terms_version: Mapped[str] = mapped_column(Text, nullable=False)
    agent_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class PaymentIntent(Base):
    __tablename__ = "payment_intents"
    __table_args__ = (
        CheckConstraint(one_of("status", PAYMENT_STATUSES), name="status"),
        CheckConstraint(one_of("recipient_kind", RECIPIENT_KINDS), name="recipient_kind"),
        CheckConstraint("amount_base_units > 0", name="amount_positive"),
        CheckConstraint("asset_decimals BETWEEN 0 AND 36", name="asset_decimals"),
        CheckConstraint(
            "recipient_kind <> 'shared_with_reference' OR payment_reference IS NOT NULL",
            name="shared_recipient_needs_reference",
        ),
        CheckConstraint(
            "num_nulls(price_source, price_usd_per_unit, price_observed_at) IN (0, 3)",
            name="price_inputs_complete",
        ),
        # A per-intent address is never reused for a second intent (DMD-15).
        Index(
            "uq_payment_intents_per_intent_recipient",
            "network",
            "recipient",
            unique=True,
            postgresql_where=text("recipient_kind = 'per_intent_address'"),
        ),
        Index(
            "uq_payment_intents_payment_reference",
            "network",
            "payment_reference",
            unique=True,
            postgresql_where=text("payment_reference IS NOT NULL"),
        ),
        Index("ix_payment_intents_status", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    registration_intent_id: Mapped[str] = mapped_column(
        ForeignKey("registration_intents.id"), nullable=False, index=True
    )
    rail_id: Mapped[str] = mapped_column(Text, nullable=False)
    adapter: Mapped[str] = mapped_column(Text, nullable=False)
    asset_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    asset_canonical_id: Mapped[str] = mapped_column(Text, nullable=False)
    asset_decimals: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    network: Mapped[str] = mapped_column(Text, nullable=False)
    amount_base_units: Mapped[Decimal] = mapped_column(TokenUnits, nullable=False)
    commercial_price_currency: Mapped[str] = mapped_column(Text, nullable=False)
    commercial_price_amount: Mapped[Decimal] = mapped_column(UsdAmount, nullable=False)
    # Conversion inputs for non-USD-denominated assets (all three set together, or none).
    price_source: Mapped[str | None] = mapped_column(Text)
    price_usd_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    price_observed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_kind: Mapped[str] = mapped_column(Text, nullable=False)
    payment_reference: Mapped[str | None] = mapped_column(Text)
    required_confirmations: Mapped[int | None] = mapped_column(Integer)
    finality_policy: Mapped[str | None] = mapped_column(Text)
    quote_expires_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class PaymentEvent(Base):
    """Append-only history of every status change and observation for a payment intent."""

    __tablename__ = "payment_events"
    __table_args__ = (
        CheckConstraint(
            f"from_status IS NULL OR {one_of('from_status', PAYMENT_STATUSES)}",
            name="from_status",
        ),
        CheckConstraint(one_of("to_status", PAYMENT_STATUSES), name="to_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    payment_intent_id: Mapped[str] = mapped_column(
        ForeignKey("payment_intents.id"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_reference: Mapped[str | None] = mapped_column(Text)
    observed_amount_base_units: Mapped[Decimal | None] = mapped_column(TokenUnits)
    confirmations: Mapped[int | None] = mapped_column(Integer)
    block_hash: Mapped[str | None] = mapped_column(Text)
    block_height: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    observed_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    recorded_at: Mapped[datetime] = created_at()


class CreditedOutput(Base):
    """Append-only ledger: each payment output can be credited to exactly one intent."""

    __tablename__ = "credited_outputs"
    __table_args__ = (CheckConstraint("amount_base_units > 0", name="amount_positive"),)

    network: Mapped[str] = mapped_column(Text, primary_key=True)
    transaction_reference: Mapped[str] = mapped_column(Text, primary_key=True)
    output_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_intent_id: Mapped[str] = mapped_column(
        ForeignKey("payment_intents.id"), nullable=False, index=True
    )
    amount_base_units: Mapped[Decimal] = mapped_column(TokenUnits, nullable=False)
    credited_at: Mapped[datetime] = created_at()


class PaymentReference(Base):
    """Append-only: transaction references submitted by payers (DMD-12).

    A reference is only a hint telling the adapter where to look; the payment is still
    verified independently on chain before anything is credited.
    """

    __tablename__ = "payment_references"
    __table_args__ = (UniqueConstraint("payment_intent_id", "reference"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    payment_intent_id: Mapped[str] = mapped_column(
        ForeignKey("payment_intents.id"), nullable=False, index=True
    )
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = created_at()


class X402Authorization(Base):
    """A signed x402 payment authorization received for a payment intent (X402-04..X402-07).

    Stored before the facilitator is asked anything, so the gateway always knows which
    (payer, nonce) it may find used on chain for this intent, even if it stopped while the
    facilitator was settling. The (network, asset, payer, nonce) key stops one authorization
    from ever being counted for two intents.
    """

    __tablename__ = "x402_authorizations"
    __table_args__ = (
        UniqueConstraint("network", "asset", "payer", "nonce"),
        CheckConstraint(one_of("status", X402_AUTHORIZATION_STATUSES), name="status"),
        CheckConstraint("value > 0", name="value_positive"),
        CheckConstraint(
            "status <> 'settled' OR settle_transaction IS NOT NULL",
            name="settled_has_transaction",
        ),
        Index(
            "uq_x402_authorizations_live_per_intent",
            "payment_intent_id",
            unique=True,
            postgresql_where=text(one_of("status", X402_LIVE_STATUSES)),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    payment_intent_id: Mapped[str] = mapped_column(
        ForeignKey("payment_intents.id"), nullable=False, index=True
    )
    network: Mapped[str] = mapped_column(Text, nullable=False)
    asset: Mapped[str] = mapped_column(Text, nullable=False)  # token contract, lower case
    pay_to: Mapped[str] = mapped_column(Text, nullable=False)  # lower case
    payer: Mapped[str] = mapped_column(Text, nullable=False)  # lower case
    nonce: Mapped[str] = mapped_column(Text, nullable=False)  # 0x + 64 hex, lower case
    value: Mapped[Decimal] = mapped_column(TokenUnits, nullable=False)
    valid_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The PaymentPayload and PaymentRequirements exactly as used with the facilitator.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requirements: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    search_from_block: Mapped[int | None] = mapped_column(BigInteger)
    settle_transaction: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class AgentIdentity(Base):
    __tablename__ = "identities"
    __table_args__ = (
        CheckConstraint(one_of("status", IDENTITY_STATUSES), name="status"),
        CheckConstraint(CONTROLLER_PRESENT, name="controller_present"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    public_identifier: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    participant_type: Mapped[str] = mapped_column(Text, nullable=False)
    controller_public_key: Mapped[str | None] = mapped_column(Text)
    controller_wallet_address: Mapped[str | None] = mapped_column(Text)
    controller_network: Mapped[str | None] = mapped_column(Text)
    registration_intent_id: Mapped[str] = mapped_column(
        ForeignKey("registration_intents.id"), nullable=False, unique=True
    )
    # The verified proof that bound the controller to this identity (ID-08).
    controller_proof_id: Mapped[str] = mapped_column(
        ForeignKey("controller_proofs.id", use_alter=True), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ControllerChallenge(Base):
    __tablename__ = "controller_challenges"
    __table_args__ = (
        CheckConstraint(one_of("purpose", CHALLENGE_PURPOSES), name="purpose"),
        CheckConstraint(
            "(registration_intent_id IS NOT NULL) <> (identity_id IS NOT NULL)",
            name="exactly_one_subject",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    registration_intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("registration_intents.id"), index=True
    )
    identity_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"), index=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    controller_type: Mapped[str] = mapped_column(Text, nullable=False)
    controller_value: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = created_at()


class ControllerProof(Base):
    """Append-only record of every answer to a controller challenge, accepted or not."""

    __tablename__ = "controller_proofs"
    __table_args__ = (
        CheckConstraint(one_of("status", PROOF_STATUSES), name="status"),
        CheckConstraint("status <> 'verified' OR verified_at IS NOT NULL", name="verified_at_set"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Unique: a challenge can be answered once, which blocks replay (TEST-08).
    challenge_id: Mapped[str] = mapped_column(
        ForeignKey("controller_challenges.id"), nullable=False, unique=True
    )
    method: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(Timestamp)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    identity_id: Mapped[str] = mapped_column(
        ForeignKey("identities.id"), nullable=False, index=True
    )
    registration_intent_id: Mapped[str] = mapped_column(
        ForeignKey("registration_intents.id"), nullable=False
    )
    payment_intent_id: Mapped[str] = mapped_column(ForeignKey("payment_intents.id"), nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    issuer_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        Timestamp, nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp)
    revocation_reason: Mapped[str | None] = mapped_column(Text)


class Entitlement(Base):
    __tablename__ = "entitlements"
    __table_args__ = (
        UniqueConstraint("subject_id", "entitlement"),
        CheckConstraint(one_of("status", IDENTITY_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), nullable=False)
    entitlement: Mapped[str] = mapped_column(Text, nullable=False)
    source_credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[datetime] = created_at()
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp)


class IdempotencyKey(Base):
    """Stored responses for mutating requests, keyed by caller-supplied Idempotency-Key (GW-06)."""

    __tablename__ = "idempotency_keys"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class AuditEvent(Base):
    """Append-only audit trail of payments, proofs, issuance, policy and operator actions."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(one_of("actor_type", ACTOR_TYPES), name="actor_type"),
        Index("ix_audit_events_subject", "subject_type", "subject_id"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = created_at()
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class PriceRecord(Base):
    """A computed price with every input it came from (append-only).

    Quotes use the latest fresh one; the public price endpoint shows it with its inputs.
    """

    __tablename__ = "price_records"
    __table_args__ = (
        CheckConstraint("usd_per_unit > 0", name="price_positive"),
        Index("ix_price_records_latest", "source", "asset_symbol", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    asset_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    network: Mapped[str] = mapped_column(Text, nullable=False)
    usd_per_unit: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at()


APPEND_ONLY_TABLES = (
    "audit_events",
    "payment_events",
    "credited_outputs",
    "controller_proofs",
    "payment_references",
    "price_records",
)
