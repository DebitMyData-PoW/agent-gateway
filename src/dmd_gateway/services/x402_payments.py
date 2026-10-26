"""Paying for a DID registration with x402 (X402-04..X402-09).

``POST /v1/dids/registrations`` is the paid resource. For a quote on an x402 rail:

1. Without a ``PAYMENT-SIGNATURE`` header it answers HTTP 402 with a ``PAYMENT-REQUIRED``
   header (x402 version 2): the quoted amount in atomic units, the token contract, the
   network and DebitMyData's receiving address, plus the Bazaar discovery description.
   Payment is only asked for once everything else is ready (controller proven, intent
   valid, issuance available), so an agent is never charged for a DID that cannot be issued.
2. With the header, the signed authorization is checked against the quote and stored, the
   facilitator verifies it, then the facilitator settles it on chain.
3. The payment adapter checks the settlement on chain itself (the same rules as every other
   rail), and once it has the required confirmations the DID is issued in the same answer,
   with a ``PAYMENT-RESPONSE`` header. If the confirmations take longer than the wait, the
   answer is 202: the agent asks again later, without paying again.

Every step is recorded in the audit trail. Signatures are never logged or audited.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.adapters.base import AdapterUnavailable, PaymentAdapter
from dmd_gateway.adapters.x402 import X402PaymentAdapter
from dmd_gateway.adapters.x402_facilitator import FacilitatorUnavailable
from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import (
    X402_LIVE_STATUSES,
    PaymentIntent,
    RegistrationIntent,
    X402Authorization,
)
from dmd_gateway.db.session import transaction
from dmd_gateway.domain.ids import EXAMPLE_REGISTRATION_INTENT_ID, REGISTRATION_INTENT_ID_PATTERN
from dmd_gateway.domain.payment_status import NEXT_ACTIONS, PaymentStatus
from dmd_gateway.domain.routes import ServiceConfig
from dmd_gateway.errors import ApiError
from dmd_gateway.services.registrations import (
    IssuanceContext,
    IssuedIdentity,
    complete_registration,
    evaluate,
    is_x402_rail,
    payment_for,
)
from dmd_gateway.services.settlement import process_intent

log = logging.getLogger(__name__)
S = PaymentStatus

X402_VERSION = 2
PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
PAYMENT_RESPONSE = "PAYMENT-RESPONSE"
MAX_HEADER_CHARS = 32_768
# No payment is taken in the last moments of a quote: it must settle before the quote ends.
MIN_QUOTE_REMAINING = timedelta(seconds=45)
SETTLEMENT_MARGIN_SECONDS = 30
# A stored authorization that never reached the facilitator (the gateway stopped in
# between) stops blocking new attempts after this long. Verification is read-only, so
# nothing can have been paid with it.
ABANDONED_AFTER = timedelta(minutes=2)
POLL_SECONDS = 2.0
# Refused payments per intent before the gateway stops asking the facilitator for this
# intent (each attempt costs a facilitator call); the agent can still request a new quote.
MAX_ATTEMPTS = 10
RETRY_AFTER_SECONDS = 10
CLOSED_PAYMENT = frozenset({S.FAILED, S.EXPIRED, S.UNDERPAID, S.REFUNDED, S.REVERSED, S.DISPUTED})

ADDRESS = r"^0x[0-9a-fA-F]{40}$"
BYTES32 = r"^0x[0-9a-fA-F]{64}$"
UINT = r"^[0-9]{1,78}$"
TRANSACTION = r"^0x[0-9a-fA-F]{64}$"


# --------------------------------------------------------------------------- payload format


def _as_text(value: Any) -> Any:
    # Numbers are accepted as JSON numbers or strings (the specification uses strings).
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class Authorization(BaseModel):
    """EIP-3009 ``TransferWithAuthorization`` parameters signed by the payer."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    payer: str = Field(alias="from", pattern=ADDRESS)
    to: str = Field(pattern=ADDRESS)
    value: str = Field(pattern=UINT)
    valid_after: str = Field(alias="validAfter", pattern=UINT)
    valid_before: str = Field(alias="validBefore", pattern=UINT)
    nonce: str = Field(pattern=BYTES32)

    @field_validator("value", "valid_after", "valid_before", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> Any:
        return _as_text(value)


class ExactEvmPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    # 65 bytes for an ordinary wallet; smart wallets (ERC-1271 / ERC-6492) send longer ones.
    signature: str = Field(pattern=r"^0x(?:[0-9a-fA-F]{2}){65,8192}$")
    authorization: Authorization


class Accepted(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    scheme: str
    network: str
    amount: str = Field(pattern=UINT)
    asset: str
    pay_to: str = Field(alias="payTo")
    max_timeout_seconds: int = Field(alias="maxTimeoutSeconds", ge=1)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("amount", mode="before")
    @classmethod
    def _number(cls, value: Any) -> Any:
        return _as_text(value)


class PaymentPayload(BaseModel):
    """x402 version 2 ``PaymentPayload`` for the "exact" scheme on EVM."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    x402_version: int = Field(alias="x402Version")
    accepted: Accepted
    payload: ExactEvmPayload
    resource: dict[str, Any] | None = None
    extensions: dict[str, Any] | None = None


def encode_header(value: Mapping[str, Any]) -> str:
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode("utf-8")).decode()


def decode_payment_header(value: str) -> dict[str, Any]:
    """The JSON object in a ``PAYMENT-SIGNATURE`` header (base64, standard or URL-safe)."""
    text = value.strip()
    if not text or len(text) > MAX_HEADER_CHARS:
        raise ValueError("the PAYMENT-SIGNATURE header is empty or too long")
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = (
            base64.urlsafe_b64decode(padded)
            if "-" in text or "_" in text
            else base64.b64decode(padded, validate=True)
        )
        data = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        message = "the PAYMENT-SIGNATURE header is not base64-encoded JSON"
        if len(text) <= 128:
            message += (
                " (an x402 payment is a long value starting with eyJ; this looks like the "
                "controller-proof signature)"
            )
        raise ValueError(message) from exc
    if not isinstance(data, dict):
        raise ValueError("the PAYMENT-SIGNATURE header must hold a JSON object")
    return data


def mismatches(
    payload: PaymentPayload,
    requirements: Mapping[str, Any],
    now: datetime,
    latest_valid_before: datetime | None = None,
) -> list[tuple[str, str]]:
    """Why a payment does not pay this quote, as (x402 error code, explanation) pairs.

    ``latest_valid_before`` is the quote's expiry: an authorization that stays usable after
    it could be settled late, when the quote no longer stands.
    """
    problems: list[tuple[str, str]] = []
    accepted = payload.accepted
    authorization = payload.payload.authorization
    if payload.x402_version != X402_VERSION:
        problems.append(("invalid_x402_version", "x402Version must be 2"))
    if accepted.scheme != requirements["scheme"]:
        problems.append(("invalid_scheme", "only the 'exact' scheme is accepted"))
    if accepted.network != requirements["network"]:
        problems.append(("invalid_network", f"the payment must be on {requirements['network']}"))
    if (
        accepted.asset.lower() != str(requirements["asset"]).lower()
        or accepted.pay_to.lower() != str(requirements["payTo"]).lower()
        or int(accepted.amount) != int(requirements["amount"])
    ):
        problems.append(
            ("invalid_payment_requirements", "'accepted' does not match this quote's requirements")
        )
    if accepted.extra.get("assetTransferMethod") not in (None, "eip3009"):
        problems.append(("invalid_payload", "only the eip3009 transfer method is accepted"))
    if authorization.to.lower() != str(requirements["payTo"]).lower():
        problems.append(
            (
                "invalid_exact_evm_payload_recipient_mismatch",
                "the authorization does not pay DebitMyData's receiving address",
            )
        )
    if int(authorization.value) != int(requirements["amount"]):
        problems.append(
            (
                "invalid_exact_evm_payload_authorization_value_mismatch",
                f"the authorization must be for exactly {requirements['amount']} atomic units",
            )
        )
    timestamp = int(now.timestamp())
    if int(authorization.valid_after) > timestamp:
        problems.append(
            (
                "invalid_exact_evm_payload_authorization_valid_after",
                "the authorization is not valid yet",
            )
        )
    if int(authorization.valid_before) < timestamp + 6:
        problems.append(
            (
                "invalid_exact_evm_payload_authorization_valid_before",
                "the authorization has expired or expires within 6 seconds; sign a new payment",
            )
        )
    elif latest_valid_before is not None and int(authorization.valid_before) > int(
        latest_valid_before.timestamp()
    ):
        problems.append(
            (
                "invalid_exact_evm_payload_authorization_valid_before",
                "the authorization must expire before the quote does "
                f"(validBefore <= {int(latest_valid_before.timestamp())})",
            )
        )
    return problems


# --------------------------------------------------------------------------- 402 contents


# The x402 Bazaar refuses descriptions over 500 characters, and lists only metadata that
# describes the endpoint (no instructions addressed to agents).
MAX_DESCRIPTION_LENGTH = 500


def resource_info(public_base_url: str, path: str, title: str) -> dict[str, Any]:
    """``ResourceInfo`` for the paid registration route, with Bazaar service metadata."""
    base = public_base_url.rstrip("/")
    info: dict[str, Any] = {
        "url": f"{base}{path}",
        "description": (
            f"{title}. Issues a DebitMyData DID (did:web), a signed W3C credential and the "
            "dmd_did_holder entitlement for a registration whose controller key or wallet "
            "has been proven. Fixed fee 25.00 USD, paid here in USDC on Base. The "
            "registration_intent_id comes from POST /v1/dids/registration-quotes, which "
            "also offers payment in native DMD."
        ),
        "mimeType": "application/json",
        "serviceName": "DebitMyData",
        "tags": ["did", "identity", "agents", "debitmydata"],
    }
    if base.startswith("https://"):
        info["iconUrl"] = f"{base}/favicon.png"
    return info


def bazaar_extension(public_base_url: str) -> dict[str, Any]:
    """Discovery description of the paid route (x402 ``bazaar`` extension, POST endpoint)."""
    host = public_base_url.split("://", 1)[-1].rstrip("/")
    return {
        "info": {
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": {"registration_intent_id": EXAMPLE_REGISTRATION_INTENT_ID},
            },
            "output": {
                "type": "json",
                "example": {
                    "status": "issued",
                    "did": f"did:web:{host}:agents:<identity id>",
                    "did_document_url": f"https://{host}/agents/<identity id>/did.json",
                    "entitlements": ["dmd_did_holder"],
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
                        "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                        "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                        "body": {
                            "type": "object",
                            "properties": {
                                "registration_intent_id": {
                                    "type": "string",
                                    "pattern": REGISTRATION_INTENT_ID_PATTERN,
                                    "description": "From POST /v1/dids/registration-quotes",
                                }
                            },
                            "required": ["registration_intent_id"],
                            "additionalProperties": False,
                        },
                        "queryParams": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
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


def payment_required(
    resource: Mapping[str, Any],
    requirements: Mapping[str, Any],
    error: str,
    public_base_url: str,
    bazaar: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The x402 ``PaymentRequired`` object; ``bazaar`` defaults to the DID registration's."""
    return {
        "x402Version": X402_VERSION,
        "error": error,
        "resource": dict(resource),
        "accepts": [dict(requirements)],
        "extensions": {"bazaar": dict(bazaar or bazaar_extension(public_base_url))},
    }


EXAMPLE_TIMEOUT_SECONDS = 300
EXAMPLE_ERROR = (
    "This request names no payable registration quote. registration_intent_id values come "
    "from POST /v1/dids/registration-quotes."
)


def example_payment_required(ctx: X402Context, service: ServiceConfig) -> ApiError | None:
    """The 402 answer to a request without a quote, or with the reserved example ID.

    x402 tools probe a paid endpoint before knowing its input: Coinbase's validator posts
    an empty body first, then the example request the gateway advertises
    (``extensions.bazaar.info.input``). Both must get a 402 with the real terms: the fixed
    price in USDC, the network and DebitMyData's receiving address. Neither names a quote,
    so a payment sent with them is refused here, before any facilitator call, and nothing
    is charged. None when no x402 rail is enabled.
    """
    for rail_id in service.payment_options:
        adapter = ctx.adapters.get(rail_id)
        if isinstance(adapter, X402PaymentAdapter) and adapter.rail.asset.usd_denominated:
            break
    else:
        return None
    amount = service.commercial_price.amount.scaleb(adapter.rail.asset.decimals)
    if amount != amount.to_integral_value():
        raise ValueError("the commercial price does not fit the asset's decimals")
    try:
        requirements = adapter.payment_requirements(
            int(amount), adapter.pay_to, EXAMPLE_TIMEOUT_SECONDS
        )
    except AdapterUnavailable:
        return _adapter_unavailable(adapter.rail_id)
    required = payment_required(
        ctx.resource, requirements, EXAMPLE_ERROR, ctx.issuance.public_base_url
    )
    return _payment_required_error(required, EXAMPLE_ERROR, code="registration_quote_required")


def payment_terms(
    adapter: X402PaymentAdapter,
    payment: PaymentIntent,
    resource: Mapping[str, Any],
    public_base_url: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """This intent's ``PaymentRequirements`` and the ``PaymentRequired`` object around them.

    Everything comes from the stored quote (amount, receiving address); the time the payer
    gets is limited so that the payment settles before the quote expires.
    """
    remaining = int((payment.quote_expires_at - now).total_seconds())
    requirements = adapter.payment_requirements(
        int(payment.amount_base_units),
        payment.recipient,
        remaining - SETTLEMENT_MARGIN_SECONDS,
    )
    required = payment_required(
        resource, requirements, f"{PAYMENT_SIGNATURE} header is required", public_base_url
    )
    return requirements, required


# --------------------------------------------------------------------------- the paid call


@dataclass(frozen=True)
class X402Context:
    factory: sessionmaker[Session]
    adapters: Mapping[str, PaymentAdapter]
    issuance: IssuanceContext
    resource: Mapping[str, Any]
    settlement_wait: timedelta
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = time.sleep


@dataclass
class PaidRegistration:
    status_code: int  # 200/201 issued, 202 still confirming
    issued: IssuedIdentity | None
    body: dict[str, Any] | None
    payment_response: dict[str, Any] | None


@dataclass(frozen=True)
class _Prepared:
    registration_intent_id: str
    payment_intent_id: str
    rail_id: str
    network: str
    authorization_id: int | None  # None: an earlier payment is already in progress
    payload: dict[str, Any] | None = None
    requirements: dict[str, Any] | None = None
    required: dict[str, Any] | None = None


def register_paid(
    ctx: X402Context, registration_intent_id: str, payment_header: str | None
) -> PaidRegistration | None:
    """Handle the x402 part of a registration call; None if the quote is not an x402 one."""
    with transaction(ctx.factory) as session:
        prepared = _prepare(session, ctx, registration_intent_id, payment_header, ctx.clock())
    if prepared is None:
        return None
    if prepared.authorization_id is not None:
        _verify_and_settle(ctx, prepared)
    return _await_settlement(ctx, prepared)


def _adapter_unavailable(rail_id: str) -> ApiError:
    return ApiError(
        503,
        "payment_rail_unavailable",
        f"Payment option {rail_id!r} is temporarily unavailable; try again later.",
        headers={"Retry-After": "60"},
    )


def _adapter(ctx: X402Context, rail_id: str) -> X402PaymentAdapter:
    adapter = ctx.adapters.get(rail_id)
    if not isinstance(adapter, X402PaymentAdapter):
        raise _adapter_unavailable(rail_id)
    return adapter


# PaymentRequired fields also put at the top of a 402 body, for x402 clients that read the
# body instead of the PAYMENT-REQUIRED header ("error" stays the gateway's error object).
PAYMENT_REQUIRED_BODY_FIELDS = ("x402Version", "resource", "accepts", "extensions")


def _payment_required_error(
    required: Mapping[str, Any], message: str, *, code: str, status: int = 402, **details: Any
) -> ApiError:
    body = dict(required)
    body["error"] = message
    return ApiError(
        status,
        code,
        message,
        details={"payment_required": body, **details},
        headers={PAYMENT_REQUIRED: encode_header(body)},
        body_extra=(
            {k: body[k] for k in PAYMENT_REQUIRED_BODY_FIELDS if k in body}
            if status == 402
            else None
        ),
    )


def _live_authorization(session: Session, payment_intent_id: str) -> X402Authorization | None:
    return session.scalars(
        select(X402Authorization)
        .where(
            X402Authorization.payment_intent_id == payment_intent_id,
            X402Authorization.status.in_(X402_LIVE_STATUSES),
        )
        .with_for_update()
    ).first()


def _prepare(
    session: Session,
    ctx: X402Context,
    registration_intent_id: str,
    header: str | None,
    now: datetime,
) -> _Prepared | None:
    # Lock order matches the payment worker: payment intent first, then the registration.
    payment = payment_for(session, registration_intent_id, lock=True)
    registration = session.get(RegistrationIntent, registration_intent_id, with_for_update=True)
    if registration is None:
        raise ApiError(404, "not_found", "Unknown registration intent.")
    if payment is None or not is_x402_rail(ctx.issuance.registry, payment.rail_id):
        return None
    state = evaluate(session, ctx.issuance, registration)
    if state.identity is not None or state.payment_status is S.SETTLED:
        return None  # nothing to pay: the ordinary path issues or returns the DID

    prepared = _Prepared(registration.id, payment.id, payment.rail_id, payment.network, None)
    live = _live_authorization(session, payment.id)
    if live is not None and live.status == "received" and live.created_at < now - ABANDONED_AFTER:
        _set_status(session, live, "verify_rejected", now, reason="abandoned before verification")
        live = None
    if live is not None:
        return prepared  # a payment is already on its way: wait for it, never pay twice

    if not ctx.issuance.available:
        raise ApiError(
            503,
            "issuance_unavailable",
            "DID issuance is not enabled on this gateway yet.",
            headers={"Retry-After": "3600"},
        )
    other_blockers = [b for b in state.blockers if not b.startswith("payment is ")]
    if other_blockers:
        raise ApiError(
            409,
            "registration_not_ready",
            "The DID cannot be issued yet, so no payment is taken.",
            details={"blockers": other_blockers, "next_action": state.next_action},
        )
    status = PaymentStatus(payment.status)
    if status is not S.AWAITING_PAYMENT:
        raise ApiError(
            409,
            "payment_not_open",
            f"This payment is {status}; it cannot be paid now.",
            details={"next_action": NEXT_ACTIONS[status]},
        )
    remaining = payment.quote_expires_at - now
    if remaining < MIN_QUOTE_REMAINING:
        raise ApiError(
            409,
            "quote_expiring",
            "This quote expires too soon to be paid; request a new quote.",
            details={"next_action": "request_new_quote"},
        )

    adapter = _adapter(ctx, payment.rail_id)
    requirements, required = payment_terms(
        adapter, payment, ctx.resource, ctx.issuance.public_base_url, now
    )
    if header is None:
        raise _payment_required_error(
            required,
            f"Payment required: answer with a {PAYMENT_SIGNATURE} header (x402 version 2).",
            code="payment_required",
            next_action="authorize_x402_payment",
        )

    try:
        payload_data = decode_payment_header(header)
        payload = PaymentPayload.model_validate(payload_data)
    except ValidationError as exc:
        raise _payment_required_error(
            required,
            "the payment payload is not a valid x402 'exact' EVM payload",
            code="invalid_payment",
            status=400,
        ) from exc
    except ValueError as exc:
        raise _payment_required_error(
            required, str(exc), code="invalid_payment", status=400
        ) from exc
    problems = mismatches(payload, requirements, now, payment.quote_expires_at)
    if problems:
        raise _payment_required_error(
            required,
            "; ".join(text for _, text in problems),
            code="payment_mismatch",
            reasons=[code for code, _ in problems],
        )

    attempts = session.scalar(
        select(func.count())
        .select_from(X402Authorization)
        .where(X402Authorization.payment_intent_id == payment.id)
    )
    if attempts >= MAX_ATTEMPTS:
        raise ApiError(
            429,
            "too_many_payment_attempts",
            "Too many refused payments for this quote; request a new quote.",
            details={"next_action": "request_new_quote"},
        )

    authorization = payload.payload.authorization
    row = X402Authorization(
        payment_intent_id=payment.id,
        network=payment.network,
        asset=adapter.token.lower(),
        pay_to=payment.recipient.lower(),
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
    try:
        session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "") or ""
        if "nonce" in constraint:
            raise ApiError(
                409,
                "authorization_already_used",
                "This payment authorization was already used; sign a new one.",
            ) from exc
        raise ApiError(
            409,
            "payment_in_progress",
            "A payment for this registration is already being processed; ask again shortly.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from exc
    record_audit_event(
        session,
        action="payment.x402_authorization_received",
        subject_type="payment_intent",
        subject_id=payment.id,
        actor_type="agent",
        data={
            "authorization_id": row.id,
            "payer": row.payer,
            "nonce": row.nonce,
            "value": str(int(authorization.value)),
            "valid_before": row.valid_before,
        },
    )
    return _Prepared(
        registration.id,
        payment.id,
        payment.rail_id,
        payment.network,
        row.id,
        payload_data,
        requirements,
        required,
    )


def _set_status(
    session: Session,
    row: X402Authorization,
    status: str,
    now: datetime,
    *,
    reason: str | None = None,
    transaction_hash: str | None = None,
    search_from_block: int | None = None,
) -> None:
    row.status = status
    row.updated_at = now
    if reason is not None:
        row.failure_reason = reason[:500]
    if transaction_hash is not None:
        row.settle_transaction = transaction_hash
    if search_from_block is not None:
        row.search_from_block = search_from_block
    data: dict[str, Any] = {"authorization_id": row.id, "status": status}
    if reason:
        data["reason"] = reason[:500]
    if row.settle_transaction:
        data["transaction"] = row.settle_transaction
    record_audit_event(
        session,
        action=f"payment.x402_{status}",
        subject_type="payment_intent",
        subject_id=row.payment_intent_id,
        actor_type="system",
        data=data,
    )


# The status an authorization must have before each change made while paying.
_PREVIOUS = {
    "verify_rejected": ("received",),
    "submitted": ("received", "submitted"),
    "settled": ("submitted",),
    "settle_rejected": ("submitted",),
}


def _update(ctx: X402Context, authorization_id: int, status: str, **changes: Any) -> None:
    with transaction(ctx.factory) as session:
        row = session.get(X402Authorization, authorization_id, with_for_update=True)
        if row is None:  # pragma: no cover - the row was committed by this request
            raise RuntimeError("x402 authorization disappeared")
        if row.status not in _PREVIOUS[status]:
            # Another request took over this authorization (for example it was set aside as
            # abandoned): stop here rather than risk settling two payments.
            raise ApiError(
                409,
                "payment_in_progress",
                "This payment is already being processed; ask again shortly.",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )
        _set_status(session, row, status, ctx.clock(), **changes)


def _verify_and_settle(ctx: X402Context, prepared: _Prepared) -> None:
    assert prepared.authorization_id is not None
    assert prepared.payload is not None and prepared.requirements is not None
    assert prepared.required is not None
    adapter = _adapter(ctx, prepared.rail_id)
    try:
        start_block = adapter.current_block()
        verdict = adapter.facilitator().verify(prepared.payload, prepared.requirements)
    except (AdapterUnavailable, FacilitatorUnavailable) as exc:
        log.warning(
            "x402 verification unavailable",
            extra={"event": {"payment_intent_id": prepared.payment_intent_id, "error": str(exc)}},
        )
        _update(ctx, prepared.authorization_id, "verify_rejected", reason="payment service down")
        raise ApiError(
            503,
            "payment_rail_unavailable",
            "The payment could not be checked right now; nothing was charged. Try again.",
            headers={"Retry-After": "30"},
        ) from exc
    if not verdict.is_valid:
        reason = verdict.invalid_reason or "the facilitator refused the payment"
        _update(ctx, prepared.authorization_id, "verify_rejected", reason=reason)
        raise _payment_required_error(
            prepared.required, f"Payment refused: {reason}", code="payment_invalid"
        )

    # Recorded before settling: if the gateway stops now, the payment is still found on chain.
    _update(ctx, prepared.authorization_id, "submitted", search_from_block=start_block)
    try:
        result = adapter.facilitator().settle(prepared.payload, prepared.requirements)
    except (AdapterUnavailable, FacilitatorUnavailable) as exc:
        log.warning(
            "x402 settlement outcome unknown; checking the chain",
            extra={"event": {"payment_intent_id": prepared.payment_intent_id, "error": str(exc)}},
        )
        return
    transaction_hash = result.transaction.lower() if result.transaction else ""
    if transaction_hash and not _is_transaction(transaction_hash):
        transaction_hash = ""
    if result.success and transaction_hash:
        _update(ctx, prepared.authorization_id, "settled", transaction_hash=transaction_hash)
    elif transaction_hash:
        # Broadcast, but the facilitator could not confirm it: the chain will tell.
        _update(
            ctx,
            prepared.authorization_id,
            "submitted",
            transaction_hash=transaction_hash,
            reason=result.error_reason,
        )
    elif result.success:
        _update(ctx, prepared.authorization_id, "submitted", reason="settled without a hash")
    else:
        reason = result.error_reason or "the facilitator could not settle the payment"
        _update(ctx, prepared.authorization_id, "settle_rejected", reason=reason)
        raise _payment_required_error(
            prepared.required, f"Payment failed: {reason}", code="payment_failed"
        )


# Shared with other paid resources (donations), which have no registration.
def verify_and_settle(
    ctx: X402Context,
    *,
    payment_intent_id: str,
    rail_id: str,
    network: str,
    authorization_id: int,
    payload: dict[str, Any],
    requirements: dict[str, Any],
    required: dict[str, Any],
) -> None:
    """Have the facilitator verify, then settle, a stored authorization (see _prepare)."""
    _verify_and_settle(
        ctx,
        _Prepared(
            "",
            payment_intent_id,
            rail_id,
            network,
            authorization_id,
            payload,
            requirements,
            required,
        ),
    )


payment_required_error = _payment_required_error


def settlement_response(session: Session, payment_intent_id: str) -> dict[str, Any] | None:
    """The PAYMENT-RESPONSE object of a payment intent's settlement, if any."""
    return _payment_response(session, payment_intent_id)


def _is_transaction(value: str) -> bool:
    return (
        len(value) == 66
        and value.startswith("0x")
        and all(c in "0123456789abcdef" for c in value[2:])
    )


def _payment_response(session: Session, payment_intent_id: str) -> dict[str, Any] | None:
    row = session.scalars(
        select(X402Authorization)
        .where(
            X402Authorization.payment_intent_id == payment_intent_id,
            X402Authorization.settle_transaction.is_not(None),
        )
        .order_by(X402Authorization.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "success": True,
        "transaction": row.settle_transaction,
        "network": row.network,
        "payer": row.payer,
    }


def _await_settlement(ctx: X402Context, prepared: _Prepared) -> PaidRegistration:
    deadline = ctx.clock() + ctx.settlement_wait
    while True:
        try:
            with transaction(ctx.factory) as session:
                process_intent(session, prepared.payment_intent_id, ctx.adapters, ctx.clock())
        except AdapterUnavailable as exc:
            log.warning(
                "x402 settlement check unavailable",
                extra={
                    "event": {"payment_intent_id": prepared.payment_intent_id, "error": str(exc)}
                },
            )
        with transaction(ctx.factory) as session:
            payment = session.get(PaymentIntent, prepared.payment_intent_id)
            assert payment is not None
            status = PaymentStatus(payment.status)
            response = _payment_response(session, payment.id)
            required = payment.required_confirmations
            last_transactions = [response["transaction"]] if response else []
        if status is S.SETTLED:
            with transaction(ctx.factory) as session:
                issued = complete_registration(
                    session, ctx.issuance, prepared.registration_intent_id, ctx.clock()
                )
                session.flush()
            return PaidRegistration(201 if issued.created else 200, issued, None, response)
        if status in CLOSED_PAYMENT:
            raise ApiError(
                409,
                "payment_failed",
                f"The payment for this registration is {status}.",
                details={"next_action": NEXT_ACTIONS[status]},
            )
        if ctx.clock() >= deadline:
            body = {
                "registration_intent_id": prepared.registration_intent_id,
                "payment_intent_id": prepared.payment_intent_id,
                "status": "payment_confirming",
                "payment_status": status.value,
                "required_confirmations": required,
                "transactions": last_transactions,
                "next_action": "register_did",
                "note": (
                    "The payment was received and is being confirmed on chain. Call "
                    "POST /v1/dids/registrations again in a few seconds, without paying again."
                ),
            }
            return PaidRegistration(202, None, body, response)
        ctx.sleep(POLL_SECONDS)
