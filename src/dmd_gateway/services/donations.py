"""Donations paid with x402: the inference donation (agents' counterpart of "Inference $").

``POST /v1/donations/inference`` is the paid resource. The donor chooses the amount (within
the registry's range) and pays in one call, without a quote:

1. Without a ``PAYMENT-SIGNATURE`` header the answer is HTTP 402 with the x402 payment
   requirements for that amount: USDC on Base to DebitMyData's inference wallet. A call
   without an amount gets the terms for an example amount, so that x402 tools (Coinbase's
   Bazaar validator) can read the route; such a call can never be paid.
2. With the header, the signed authorization is checked against those requirements, stored
   with a new purchase and its payment intent, verified and settled by the facilitator.
3. The payment is checked on chain exactly like a DID payment (same adapter, worker and
   rules); once settled, the purchase is completed with a receipt signed by the issuer key.

Sending the same signed payment again returns the same donation: it is never paid twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.adapters.base import AdapterUnavailable, PaymentAdapter
from dmd_gateway.adapters.x402 import X402PaymentAdapter
from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import (
    CreditedOutput,
    PaymentEvent,
    PaymentIntent,
    Purchase,
    X402Authorization,
)
from dmd_gateway.db.session import transaction
from dmd_gateway.domain.ids import PAYMENT_INTENT, PURCHASE, new_id
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.domain.routes import DonationConfig, RailConfig
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.credentials import DonationFacts, donation_receipt
from dmd_gateway.services.registrations import IssuanceContext
from dmd_gateway.services.settlement import process_intent
from dmd_gateway.services.x402_payments import (
    CLOSED_PAYMENT,
    PAYMENT_SIGNATURE,
    POLL_SECONDS,
    SETTLEMENT_MARGIN_SECONDS,
    PaymentPayload,
    X402Context,
    decode_payment_header,
    mismatches,
    payment_required,
    payment_required_error,
    settlement_response,
    verify_and_settle,
)

S = PaymentStatus

PURCHASE_ID_PATTERN = r"^dmd_purchase_[0-9A-HJKMNP-TV-Z]{26}$"
AMOUNT_PATTERN = r"^(0|[1-9][0-9]{0,8})(\.[0-9]{1,2})?$"
DID_PATTERN = r"^did:[a-z0-9]+:[A-Za-z0-9._:%-]{1,200}$"
# How long a donation's payment may take; the signed authorization must expire before.
DONATION_WINDOW = timedelta(minutes=15)
PAYMENT_TIMEOUT_SECONDS = 300
EXAMPLE_AMOUNT = Decimal("1.00")
EXAMPLE_ERROR = (
    "This request names no amount, so it cannot be paid: these are the terms for an example "
    "of {amount} USD. Donations take amount_usd from {minimum} to {maximum}."
)


class DonationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"examples": [{"amount_usd": "5.00", "donor_name": "ResearchClaw-01"}]},
    )

    amount_usd: str | None = Field(
        default=None,
        pattern=AMOUNT_PATTERN,
        description='The donation in US dollars, e.g. "5.00" (paid as the same amount of USDC).',
    )
    donor_name: str | None = Field(
        default=None, min_length=1, max_length=80, description="Optional, shown in the receipt."
    )
    donor_did: str | None = Field(
        default=None,
        pattern=DID_PATTERN,
        description="Optional DID of the donor; the receipt is issued to it. Otherwise the "
        "receipt names the paying wallet.",
    )


@dataclass(frozen=True)
class DonationContext:
    factory: sessionmaker[Session]
    adapters: Mapping[str, PaymentAdapter]
    issuance: IssuanceContext
    settlement_wait: timedelta
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = time.sleep

    @property
    def base(self) -> str:
        return self.issuance.public_base_url.rstrip("/")


@dataclass
class DonationResult:
    status_code: int  # 201 completed now, 200 completed earlier, 202 still confirming
    body: dict[str, Any]
    payment_response: dict[str, Any] | None


# --------------------------------------------------------------------------- discovery


def donation_resource(base: str, donation: DonationConfig) -> dict[str, Any]:
    """``ResourceInfo`` of the donation route (Bazaar: description of 500 characters at most,
    describing the endpoint rather than instructing agents)."""
    info: dict[str, Any] = {
        "url": f"{base}{donation.path}",
        "description": " ".join(donation.description.split()),
        "mimeType": "application/json",
        "serviceName": "DebitMyData",
        "tags": ["donation", "inference", "ai", "debitmydata"],
    }
    if base.startswith("https://"):
        info["iconUrl"] = f"{base}/favicon.png"
    return info


def donation_bazaar(base: str, donation: DonationConfig) -> dict[str, Any]:
    """Discovery description of the donation route (x402 ``bazaar`` extension)."""
    return {
        "info": {
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": {"amount_usd": f"{EXAMPLE_AMOUNT:.2f}"},
            },
            "output": {
                "type": "json",
                "example": {
                    "status": "completed",
                    "purchase_id": "dmd_purchase_<id>",
                    "amount": {"currency": "USD", "amount": f"{EXAMPLE_AMOUNT:.2f}"},
                    "status_url": f"{base}/v1/donations/dmd_purchase_<id>",
                    "receipt": {"type": ["VerifiableCredential", "DebitMyDataDonationReceipt"]},
                },
            },
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["POST"]},
                        "bodyType": {"type": "string", "enum": ["json"]},
                        "body": {
                            "type": "object",
                            "properties": {
                                "amount_usd": {
                                    "type": "string",
                                    "pattern": AMOUNT_PATTERN,
                                    "description": (
                                        f"USD amount from {donation.amount.minimum} to "
                                        f"{donation.amount.maximum}"
                                    ),
                                },
                                "donor_name": {"type": "string", "maxLength": 80},
                                "donor_did": {"type": "string", "pattern": DID_PATTERN},
                            },
                            "required": ["amount_usd"],
                            "additionalProperties": False,
                        },
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["type", "method", "bodyType", "body"],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": {"type": "object"},
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }


# --------------------------------------------------------------------------- the paid call


def _option(
    ctx: DonationContext, donation_id: str
) -> tuple[str, RailConfig, X402PaymentAdapter, str]:
    for rail_id, rail, recipient in ctx.issuance.registry.donation_rails(donation_id):
        adapter = ctx.adapters.get(rail_id)
        if isinstance(adapter, X402PaymentAdapter):
            return rail_id, rail, adapter, recipient
    raise ApiError(
        503,
        "donations_unavailable",
        "Donations are not open on this gateway yet; nothing was charged.",
        headers={"Retry-After": "3600"},
    )


def _amount(raw: str | None, donation: DonationConfig) -> Decimal | None:
    if raw is None:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - the pattern already refused it
        raise ApiError(422, "invalid_amount", "amount_usd is not a decimal number.") from exc
    low, high = donation.amount.minimum, donation.amount.maximum
    if not low <= amount <= high:
        raise ApiError(
            422,
            "invalid_amount",
            f"amount_usd must be from {low} to {high} USD.",
            details={"minimum": f"{low:.2f}", "maximum": f"{high:.2f}"},
        )
    return amount.quantize(Decimal("0.01"))


def _units(amount: Decimal, rail: RailConfig) -> int:
    units = amount.scaleb(rail.asset.decimals)
    if units != units.to_integral_value():  # pragma: no cover - 2 decimals always fit USDC
        raise ApiError(422, "invalid_amount", "amount_usd has too many decimals.")
    return int(units)


def _x402(ctx: DonationContext) -> X402Context:
    return X402Context(
        factory=ctx.factory,
        adapters=ctx.adapters,
        issuance=ctx.issuance,
        resource={},
        settlement_wait=ctx.settlement_wait,
        clock=ctx.clock,
        sleep=ctx.sleep,
    )


def _check_open(ctx: DonationContext, donation_id: str) -> None:
    _option(ctx, donation_id)
    if not ctx.issuance.available:
        raise ApiError(
            503,
            "receipts_unavailable",
            "Donation receipts cannot be signed on this gateway yet; nothing was charged.",
            headers={"Retry-After": "3600"},
        )


def donation_payment_terms(
    ctx: DonationContext, donation_id: str, amount_usd: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The x402 requirements and the PaymentRequired that a donation of this amount answers
    with (for the staging test payer, which signs them like an agent's wallet would)."""
    _check_open(ctx, donation_id)
    donation = ctx.issuance.registry.donations[donation_id]
    rail_id, rail, adapter, recipient = _option(ctx, donation_id)
    amount = _amount(amount_usd, donation)
    assert amount is not None
    requirements = adapter.payment_requirements(
        _units(amount, rail), recipient, PAYMENT_TIMEOUT_SECONDS
    )
    required = payment_required(
        donation_resource(ctx.base, donation),
        requirements,
        f"{PAYMENT_SIGNATURE} header is required",
        ctx.base,
        donation_bazaar(ctx.base, donation),
    )
    return requirements, required


def donate(
    ctx: DonationContext,
    donation_id: str,
    request: DonationRequest | None,
    header: str | None,
) -> DonationResult:
    _check_open(ctx, donation_id)
    donation = ctx.issuance.registry.donations[donation_id]
    rail_id, rail, adapter, recipient = _option(ctx, donation_id)
    resource = donation_resource(ctx.base, donation)
    bazaar = donation_bazaar(ctx.base, donation)
    amount = _amount(request.amount_usd if request else None, donation)

    if amount is None:  # a probe: show the terms of an example amount, never take a payment
        message = EXAMPLE_ERROR.format(
            amount=f"{EXAMPLE_AMOUNT:.2f}",
            minimum=f"{donation.amount.minimum:.2f}",
            maximum=f"{donation.amount.maximum:.2f}",
        )
        requirements = adapter.payment_requirements(
            _units(EXAMPLE_AMOUNT, rail), recipient, PAYMENT_TIMEOUT_SECONDS
        )
        raise payment_required_error(
            payment_required(resource, requirements, message, ctx.base, bazaar),
            message,
            code="donation_amount_required",
        )

    requirements, required = donation_payment_terms(ctx, donation_id, f"{amount:.2f}")
    if header is None:
        raise payment_required_error(
            required,
            f"Payment required: answer with a {PAYMENT_SIGNATURE} header (x402 version 2).",
            code="payment_required",
            next_action="authorize_x402_payment",
        )
    try:
        payload_data = decode_payment_header(header)
        payload = PaymentPayload.model_validate(payload_data)
    except ValidationError as exc:
        raise payment_required_error(
            required,
            "the payment payload is not a valid x402 'exact' EVM payload",
            code="invalid_payment",
            status=400,
        ) from exc
    except ValueError as exc:
        raise payment_required_error(
            required, str(exc), code="invalid_payment", status=400
        ) from exc

    now = ctx.clock()
    authorization = payload.payload.authorization
    with transaction(ctx.factory) as session:
        earlier = session.scalars(
            select(X402Authorization).where(
                X402Authorization.network == rail.network,
                X402Authorization.asset == adapter.token.lower(),
                X402Authorization.payer == authorization.payer.lower(),
                X402Authorization.nonce == authorization.nonce.lower(),
            )
        ).first()
        if earlier is not None:
            intent = session.get(PaymentIntent, earlier.payment_intent_id)
            purchase = session.get(Purchase, intent.purchase_id) if intent else None
            if (
                intent is None
                or purchase is None
                or purchase.product_id != donation_id
                or int(earlier.value) != int(requirements["amount"])
            ):
                raise ApiError(
                    409,
                    "authorization_already_used",
                    "This payment authorization was already used; sign a new one.",
                )
            # The same signed payment sent again: the same donation, never a second payment.
            purchase_id, payment_intent_id = purchase.id, intent.id
            replay = True
        else:
            replay = False
            expires_at = now + DONATION_WINDOW
            latest = expires_at - timedelta(seconds=SETTLEMENT_MARGIN_SECONDS)
            problems = mismatches(payload, requirements, now, latest)
            if problems:
                raise payment_required_error(
                    required,
                    "; ".join(text for _, text in problems),
                    code="payment_mismatch",
                    reasons=[code for code, _ in problems],
                )
            purchase_id, payment_intent_id, authorization_id = _record(
                session,
                donation_id=donation_id,
                donation=donation,
                rail_id=rail_id,
                rail=rail,
                adapter=adapter,
                recipient=recipient,
                amount=amount,
                request=request,
                payload=payload,
                payload_data=payload_data,
                requirements=requirements,
                now=now,
                expires_at=expires_at,
            )
    if not replay:
        try:
            verify_and_settle(
                _x402(ctx),
                payment_intent_id=payment_intent_id,
                rail_id=rail_id,
                network=rail.network,
                authorization_id=authorization_id,
                payload=payload_data,
                requirements=requirements,
                required=required,
            )
        except ApiError:
            _fail_unless_settling(ctx, purchase_id, authorization_id)
            raise
    return _await(ctx, purchase_id, payment_intent_id, replay=replay)


def _record(
    session: Session,
    *,
    donation_id: str,
    donation: DonationConfig,
    rail_id: str,
    rail: RailConfig,
    adapter: X402PaymentAdapter,
    recipient: str,
    amount: Decimal,
    request: DonationRequest | None,
    payload: PaymentPayload,
    payload_data: dict[str, Any],
    requirements: dict[str, Any],
    now: datetime,
    expires_at: datetime,
) -> tuple[str, str, int]:
    """Store the purchase, its payment intent and the authorization, before any call out."""
    purchase_id, payment_id = new_id(PURCHASE), new_id(PAYMENT_INTENT)
    session.add(
        Purchase(
            id=purchase_id,
            product_id=donation_id,
            commercial_price_currency=donation.amount.currency,
            commercial_price_amount=amount,
            donor_name=request.donor_name if request else None,
            donor_did=request.donor_did if request else None,
            terms_version=donation.terms_version,
            status="awaiting_payment",
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()
    session.add(
        PaymentIntent(
            id=payment_id,
            purchase_id=purchase_id,
            rail_id=rail_id,
            adapter=rail.adapter,
            asset_symbol=rail.asset.symbol,
            asset_canonical_id=rail.asset.canonical_id,
            asset_decimals=rail.asset.decimals,
            network=rail.network,
            amount_base_units=Decimal(int(requirements["amount"])),
            commercial_price_currency=donation.amount.currency,
            commercial_price_amount=amount,
            recipient=recipient,
            recipient_kind="shared_with_reference",
            payment_reference=payment_id,
            required_confirmations=rail.confirmation_policy.required_confirmations,
            finality_policy=rail.confirmation_policy.finality,
            quote_expires_at=expires_at,
            status=S.AWAITING_PAYMENT.value,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()
    session.add(
        PaymentEvent(
            payment_intent_id=payment_id,
            from_status=None,
            to_status=S.AWAITING_PAYMENT.value,
            detail={"event": "donation_received"},
            observed_at=now,
        )
    )
    authorization = payload.payload.authorization
    row = X402Authorization(
        payment_intent_id=payment_id,
        network=rail.network,
        asset=adapter.token.lower(),
        pay_to=recipient.lower(),
        payer=authorization.payer.lower(),
        nonce=authorization.nonce.lower(),
        value=Decimal(int(authorization.value)),
        valid_after=int(authorization.valid_after),
        valid_before=int(authorization.valid_before),
        payload=payload_data,
        requirements=requirements,
        status="received",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    record_audit_event(
        session,
        action="purchase.created",
        subject_type="purchase",
        subject_id=purchase_id,
        actor_type="agent",
        data={
            "product_id": donation_id,
            "amount_usd": f"{amount:.2f}",
            "payment_intent_id": payment_id,
            "rail_id": rail_id,
            "recipient": recipient,
        },
    )
    record_audit_event(
        session,
        action="payment.x402_authorization_received",
        subject_type="payment_intent",
        subject_id=payment_id,
        actor_type="agent",
        data={
            "authorization_id": row.id,
            "payer": row.payer,
            "nonce": row.nonce,
            "value": str(int(authorization.value)),
            "valid_before": row.valid_before,
        },
    )
    return purchase_id, payment_id, row.id


def _fail_unless_settling(ctx: DonationContext, purchase_id: str, authorization_id: int) -> None:
    """A payment refused before settlement moved no funds: its donation is closed at once.
    Anything that reached settlement keeps being watched on chain."""
    with transaction(ctx.factory) as session:
        row = session.get(X402Authorization, authorization_id)
        purchase = session.get(Purchase, purchase_id, with_for_update=True)
        if row is None or purchase is None or row.status != "verify_rejected":
            return
        if purchase.status == "awaiting_payment":
            purchase.status = "failed"
            purchase.updated_at = ctx.clock()
            record_audit_event(
                session,
                action="purchase.status_changed",
                subject_type="purchase",
                subject_id=purchase.id,
                actor_type="system",
                data={"from": "awaiting_payment", "to": "failed", "reason": row.failure_reason},
            )


def _await(
    ctx: DonationContext, purchase_id: str, payment_intent_id: str, *, replay: bool
) -> DonationResult:
    deadline = ctx.clock() + ctx.settlement_wait
    while True:
        try:
            with transaction(ctx.factory) as session:
                process_intent(session, payment_intent_id, ctx.adapters, ctx.clock())
        except AdapterUnavailable:
            pass  # checked again below and by the worker
        with transaction(ctx.factory) as session:
            completed_before = _status(session, purchase_id) == "completed"
            purchase = complete_purchase(session, ctx, purchase_id)
            body = donation_body(session, ctx.base, purchase)
            response = settlement_response(session, payment_intent_id)
            status = PaymentStatus(
                session.get(PaymentIntent, payment_intent_id).status  # type: ignore[union-attr]
            )
        if purchase.status == "completed":
            code = 200 if (replay and completed_before) else 201
            return DonationResult(code, body, response)
        if purchase.status == "failed" or status in CLOSED_PAYMENT:
            raise ApiError(
                409,
                "payment_failed",
                f"The payment for this donation is {status}; nothing was received.",
                details={"purchase_id": purchase_id, "status_url": body["status_url"]},
            )
        if ctx.clock() >= deadline:
            return DonationResult(202, body, response)
        ctx.sleep(POLL_SECONDS)


def _status(session: Session, purchase_id: str) -> str | None:
    purchase = session.get(Purchase, purchase_id)
    return purchase.status if purchase else None


# --------------------------------------------------------------------------- completion


def complete_purchase(session: Session, ctx: DonationContext, purchase_id: str) -> Purchase:
    """Issue the receipt of a paid donation (idempotent); returns the purchase as it is."""
    purchase = session.get(Purchase, purchase_id, with_for_update=True)
    if purchase is None:
        raise ApiError(404, "not_found", "Unknown donation.")
    if purchase.status != "paid" or ctx.issuance.issuer is None:
        return purchase
    intent = session.scalars(
        select(PaymentIntent).where(PaymentIntent.purchase_id == purchase.id)
    ).one()
    authorization = session.scalars(
        select(X402Authorization)
        .where(
            X402Authorization.payment_intent_id == intent.id,
            X402Authorization.status.in_(("submitted", "settled", "settle_rejected")),
        )
        .order_by(X402Authorization.id.desc())
        .limit(1)
    ).one()
    credited = list(
        session.scalars(
            select(CreditedOutput.transaction_reference).where(
                CreditedOutput.payment_intent_id == intent.id
            )
        )
    )
    donation = ctx.issuance.registry.donations[purchase.product_id]
    now = ctx.clock()
    purchase.receipt = donation_receipt(
        DonationFacts(
            receipt_url=f"{ctx.base}/v1/donations/{purchase.id}",
            purchase_id=purchase.id,
            purpose=donation.title,
            currency=purchase.commercial_price_currency,
            amount=f"{purchase.commercial_price_amount:.2f}",
            terms_version=purchase.terms_version,
            payment_intent_id=intent.id,
            payment_option=intent.rail_id,
            asset=intent.asset_symbol,
            network=intent.network,
            amount_paid=format_base_units(int(intent.amount_base_units), intent.asset_decimals),
            payer_account=f"{intent.network}:{authorization.payer}",
            recipient=intent.recipient,
            transactions=sorted(set(credited)),
            issued_at=now,
            donor_name=purchase.donor_name,
            donor_did=purchase.donor_did,
        ),
        ctx.issuance.issuer,
    )
    purchase.status = "completed"
    purchase.completed_at = now
    purchase.updated_at = now
    record_audit_event(
        session,
        action="purchase.completed",
        subject_type="purchase",
        subject_id=purchase.id,
        actor_type="system",
        data={"payment_intent_id": intent.id, "transactions": sorted(set(credited))},
    )
    session.flush()
    return purchase


def donation_body(session: Session, base: str, purchase: Purchase) -> dict[str, Any]:
    intent = session.scalars(
        select(PaymentIntent).where(PaymentIntent.purchase_id == purchase.id)
    ).one()
    status = PaymentStatus(intent.status)
    shown = {
        "awaiting_payment": "payment_confirming",
        "paid": "payment_confirming",
        "completed": "completed",
        "failed": "failed",
    }[purchase.status]
    response = settlement_response(session, intent.id)
    return {
        "purchase_id": purchase.id,
        "product_id": purchase.product_id,
        "status": shown,
        "amount": {
            "currency": purchase.commercial_price_currency,
            "amount": f"{purchase.commercial_price_amount:.2f}",
        },
        "payment": {
            "payment_intent_id": intent.id,
            "payment_option": intent.rail_id,
            "asset": intent.asset_symbol,
            "network": intent.network,
            "amount": format_base_units(int(intent.amount_base_units), intent.asset_decimals),
            "recipient": intent.recipient,
            "status": status.value,
            "required_confirmations": intent.required_confirmations,
            "transaction": response["transaction"] if response else None,
            "payer": response["payer"] if response else None,
        },
        "receipt": purchase.receipt,
        "status_url": f"{base}/v1/donations/{purchase.id}",
        "created_at": purchase.created_at.isoformat(),
        "completed_at": purchase.completed_at.isoformat() if purchase.completed_at else None,
    }


def donation_status(ctx: DonationContext, purchase_id: str) -> dict[str, Any]:
    """A donation as it stands; a paid one gets its receipt now if it had none yet."""
    with transaction(ctx.factory) as session:
        purchase = complete_purchase(session, ctx, purchase_id)
        if purchase.product_id not in ctx.issuance.registry.donations:
            raise ApiError(404, "not_found", "Unknown donation.")
        return donation_body(session, ctx.base, purchase)
