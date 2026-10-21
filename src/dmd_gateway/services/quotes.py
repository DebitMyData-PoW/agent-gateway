"""DID registration quotes (QUOTE-05..12).

Follows the directive's Payment Quote API: the agent names an approved payment option and
gets back a time-limited quote stating the fixed 25.00 USD price, the exact amount in the
chosen asset, the network and asset identity, the DebitMyData payment destination, the
confirmation/finality policy, the payment-intent ID, the expiry, the conversion source and
time (non-USD assets only) and the applicable policies.

A quote never switches the payer to another asset, never charges an unquoted amount, and is
refused rather than computed from a price that fails the rail's price policy.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from dmd_gateway.adapters.base import AdapterUnavailable, PaymentAdapter
from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import (
    RECIPIENT_KINDS,
    PaymentEvent,
    PaymentIntent,
    RegistrationIntent,
)
from dmd_gateway.domain.ids import PAYMENT_INTENT, REGISTRATION_INTENT, new_id
from dmd_gateway.domain.money import format_base_units, to_base_units, usd_to_base_units
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.domain.routes import PaymentRouteRegistry, RailConfig, RailState
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import ControllerKeyError, parse_controller_key
from dmd_gateway.pricing import PriceObservation, PriceSource, PriceUnavailable, select_price

log = logging.getLogger(__name__)

DID_REGISTRATION = "did_registration"
METADATA_MAX_KEYS = 20
METADATA_MAX_TEXT = 256

_NO_CONTROL_CHARS = re.compile(r"^[^\x00-\x1f\x7f]+$")


class ParticipantType(StrEnum):
    """Participants named in the directive."""

    AUTONOMOUS_AGENT = "autonomous_agent"
    AI_AGENT = "ai_agent"
    BOT = "bot"
    AGENTICBOT = "agenticbot"
    CLAW = "claw"
    DIGITAL_TWIN = "digital_twin"
    HUMAN = "human"
    ORGANIZATION = "organization"
    APPROVED_DIGITAL_ENTITY = "approved_digital_entity"


MetadataValue = str | int | bool | None


class QuoteRequest(BaseModel):
    """Request body, as in the directive's example request."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "display_name": "TestAgent-01",
                    "participant_type": "autonomous_agent",
                    "controller_public_key": "ed25519:z6Mk<your agent public key>",
                    "preferred_payment_option": "dmd_pow_native",
                }
            ]
        },
    )

    display_name: str = Field(min_length=1, max_length=100, pattern=_NO_CONTROL_CHARS.pattern)
    participant_type: ParticipantType
    controller_public_key: str | None = Field(
        default=None, max_length=256, pattern=r"^[a-z0-9-]{2,32}:[A-Za-z0-9+/=_.:-]{8,220}$"
    )
    controller_wallet_address: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9:_.-]{8,128}$"
    )
    preferred_payment_option: str = Field(min_length=1, max_length=64)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @field_validator("controller_public_key")
    @classmethod
    def _valid_controller_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return parse_controller_key(value).canonical
        except ControllerKeyError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _controller_required(self) -> QuoteRequest:
        if not self.controller_public_key and not self.controller_wallet_address:
            raise ValueError(
                "controller_public_key or controller_wallet_address is required "
                "(the controller of the DID)"
            )
        if not self.controller_public_key:
            # Paying for a registration that can never be completed would only lead to a
            # refund, so a controller that cannot prove control yet is refused up front.
            raise ValueError(
                "controller_public_key is required: wallet-control proofs are not available "
                "yet, so the DID controller must be an ed25519 public key"
            )
        return self

    @field_validator("metadata")
    @classmethod
    def _small_metadata(cls, value: dict[str, MetadataValue]) -> dict[str, MetadataValue]:
        if len(value) > METADATA_MAX_KEYS:
            raise ValueError(f"metadata may have at most {METADATA_MAX_KEYS} keys")
        for key, item in value.items():
            if not 1 <= len(key) <= 64 or not _NO_CONTROL_CHARS.match(key):
                raise ValueError("metadata keys must be 1-64 printable characters")
            if isinstance(item, str) and len(item) > METADATA_MAX_TEXT:
                raise ValueError(f"metadata values may be at most {METADATA_MAX_TEXT} characters")
        return value


class Money(BaseModel):
    currency: str
    amount: str


class SelectedPaymentOption(BaseModel):
    id: str
    name: str
    asset: str
    asset_type: str
    asset_id: str
    token_contract: str | None
    network: str
    payment_protocol: str
    scheme: str | None
    transfer_method: str | None
    amount: str
    amount_base_units: str
    decimals: int
    recipient: str
    payment_reference: str | None
    required_confirmations: int | None
    finality: str | None


class PriceQuote(BaseModel):
    source: str
    usd_per_unit: str
    price_observed_at: datetime
    quoted_at: datetime
    expires_at: datetime


class RegistrationQuote(BaseModel):
    registration_intent_id: str
    payment_intent_id: str
    service: str
    commercial_price: Money
    selected_payment_option: SelectedPaymentOption
    price_quote: PriceQuote | None
    payment_status: PaymentStatus
    next_action: str
    expires_at: datetime
    controller_proof_required: bool
    terms_version: str
    policy_urls: dict[str, str]


@dataclass(frozen=True)
class QuoteContext:
    registry: PaymentRouteRegistry
    adapters: Mapping[str, PaymentAdapter]  # rail id -> adapter, enabled rails only
    price_sources: Mapping[str, PriceSource]
    quote_ttl: timedelta


def enabled_option_ids(ctx: QuoteContext, service_id: str) -> list[str]:
    return [rail_id for rail_id, _ in ctx.registry.enabled_rails(service_id)]


def resolve_payment_option(ctx: QuoteContext, service_id: str, rail_id: str) -> RailConfig:
    """The chosen rail, if it is offered for this service and currently enabled."""
    service = ctx.registry.services[service_id]
    enabled = enabled_option_ids(ctx, service_id)
    if rail_id not in service.payment_options:
        raise ApiError(
            422,
            "unknown_payment_option",
            f"{rail_id!r} is not a payment option for this service.",
            details={"enabled_payment_options": enabled},
        )
    if ctx.registry.rail_state(rail_id) is not RailState.ENABLED or rail_id not in ctx.adapters:
        raise ApiError(
            409,
            "payment_option_unavailable",
            f"Payment option {rail_id!r} is not currently available.",
            details={"enabled_payment_options": enabled},
        )
    return ctx.registry.rails[rail_id]


def _quote_amount(
    ctx: QuoteContext, rail: RailConfig, usd_price: Decimal, now: datetime
) -> tuple[int, PriceObservation | None]:
    decimals = rail.asset.decimals
    if rail.asset.usd_denominated:
        return to_base_units(usd_price, decimals), None
    try:
        observation = select_price(rail.price_policy, rail.asset.symbol, ctx.price_sources, now)
    except PriceUnavailable as exc:
        raise ApiError(
            503,
            "price_unavailable",
            "No approved price is available for this asset right now; try again later.",
            headers={"Retry-After": "60"},
        ) from exc
    return usd_to_base_units(usd_price, observation.usd_per_unit, decimals), observation


def _token_contract(rail: RailConfig) -> str | None:
    if rail.asset.asset_type == "native" or not rail.asset.canonical_id:
        return None
    return rail.asset.canonical_id.rsplit(":", 1)[-1]


def _next_action(rail: RailConfig) -> str:
    return "authorize_x402_payment" if rail.protocol == "x402" else "send_payment"


def create_registration_quote(
    session: Session,
    ctx: QuoteContext,
    request: QuoteRequest,
    now: datetime,
    *,
    service_id: str = DID_REGISTRATION,
) -> RegistrationQuote:
    """Create the registration and payment intents and return the quote.

    Runs inside the caller's transaction: either everything below is stored (intents,
    first payment event, audit event) or nothing is.
    """
    service = ctx.registry.services[service_id]
    rail_id = request.preferred_payment_option
    rail = resolve_payment_option(ctx, service_id, rail_id)
    usd_price = service.commercial_price.amount
    amount_units, observation = _quote_amount(ctx, rail, usd_price, now)
    if amount_units <= 0:
        raise ApiError(500, "quote_failed", "The quote could not be calculated.")

    expires_at = now + ctx.quote_ttl
    registration_id = new_id(REGISTRATION_INTENT)
    payment_id = new_id(PAYMENT_INTENT)

    session.add(
        RegistrationIntent(
            id=registration_id,
            service_id=service_id,
            display_name=request.display_name,
            participant_type=request.participant_type.value,
            controller_public_key=request.controller_public_key,
            controller_wallet_address=request.controller_wallet_address,
            commercial_price_currency=service.commercial_price.currency,
            commercial_price_amount=usd_price,
            terms_version=service.terms_version,
            agent_metadata=dict(request.metadata),
            status="awaiting_payment",
            expires_at=expires_at,
        )
    )
    session.flush()

    # The destination is fixed and stored before the quote is returned (DMD-03).
    try:
        destination = ctx.adapters[rail_id].prepare_destination(payment_id)
    except AdapterUnavailable as exc:
        log.warning(
            "payment rail unavailable for quote",
            extra={"event": {"rail": rail_id, "error": str(exc)}},
        )
        raise ApiError(
            503,
            "payment_rail_unavailable",
            f"Payment option {rail_id!r} is temporarily unavailable; try again later.",
            headers={"Retry-After": "60"},
        ) from exc
    if destination.recipient_kind not in RECIPIENT_KINDS or not destination.recipient:
        raise ApiError(500, "quote_failed", "The payment destination could not be prepared.")

    session.add(
        PaymentIntent(
            id=payment_id,
            registration_intent_id=registration_id,
            rail_id=rail_id,
            adapter=rail.adapter,
            asset_symbol=rail.asset.symbol,
            asset_canonical_id=rail.asset.canonical_id,
            asset_decimals=rail.asset.decimals,
            network=rail.network,
            amount_base_units=Decimal(amount_units),
            commercial_price_currency=service.commercial_price.currency,
            commercial_price_amount=usd_price,
            price_source=observation.source if observation else None,
            price_usd_per_unit=observation.usd_per_unit if observation else None,
            price_observed_at=observation.observed_at if observation else None,
            recipient=destination.recipient,
            recipient_kind=destination.recipient_kind,
            payment_reference=destination.payment_reference,
            required_confirmations=rail.confirmation_policy.required_confirmations,
            finality_policy=rail.confirmation_policy.finality,
            quote_expires_at=expires_at,
            status=PaymentStatus.AWAITING_PAYMENT.value,
        )
    )
    session.flush()
    session.add(
        PaymentEvent(
            payment_intent_id=payment_id,
            from_status=None,
            to_status=PaymentStatus.AWAITING_PAYMENT.value,
            detail={"event": "quote_created"},
            observed_at=now,
        )
    )
    audit_data: dict[str, Any] = {
        "payment_intent_id": payment_id,
        "rail_id": rail_id,
        "asset": rail.asset.symbol,
        "network": rail.network,
        "amount_base_units": str(amount_units),
        "commercial_price_usd": f"{usd_price:.2f}",
        "terms_version": service.terms_version,
    }
    if observation:
        audit_data["price_source"] = observation.source
        audit_data["usd_per_unit"] = str(observation.usd_per_unit)
    record_audit_event(
        session,
        action="registration_quote.created",
        subject_type="registration_intent",
        subject_id=registration_id,
        actor_type="agent",
        data=audit_data,
    )

    return RegistrationQuote(
        registration_intent_id=registration_id,
        payment_intent_id=payment_id,
        service=service.title,
        commercial_price=Money(
            currency=service.commercial_price.currency, amount=f"{usd_price:.2f}"
        ),
        selected_payment_option=SelectedPaymentOption(
            id=rail_id,
            name=rail.name,
            asset=rail.asset.symbol,
            asset_type=rail.asset.asset_type,
            asset_id=rail.asset.canonical_id or "",
            token_contract=_token_contract(rail),
            network=rail.network,
            payment_protocol=rail.protocol,
            scheme=rail.scheme,
            transfer_method=rail.transfer_method,
            amount=format_base_units(amount_units, rail.asset.decimals),
            amount_base_units=str(amount_units),
            decimals=rail.asset.decimals,
            recipient=destination.recipient,
            payment_reference=destination.payment_reference,
            required_confirmations=rail.confirmation_policy.required_confirmations,
            finality=rail.confirmation_policy.finality,
        ),
        price_quote=(
            PriceQuote(
                source=observation.source,
                usd_per_unit=str(observation.usd_per_unit),
                price_observed_at=observation.observed_at,
                quoted_at=now,
                expires_at=expires_at,
            )
            if observation
            else None
        ),
        payment_status=PaymentStatus.AWAITING_PAYMENT,
        next_action=_next_action(rail),
        expires_at=expires_at,
        controller_proof_required=True,
        terms_version=service.terms_version,
        policy_urls=dict(service.policy_urls),
    )
