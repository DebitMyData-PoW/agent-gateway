"""GET /v1/payments/{payment_intent_id}: payment status for the paying agent.

Returns the normalized status plus confirmation, settlement and refund details
(directive MCP tool ``dmd_get_payment_status``). The payment-intent ID is random and only
known to whoever requested the quote.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from dmd_gateway.db.models import PaymentEvent, PaymentIntent
from dmd_gateway.db.session import transaction
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.domain.payment_status import NEXT_ACTIONS, PaymentStatus
from dmd_gateway.errors import ApiError
from dmd_gateway.services.payment_references import X402_ADAPTER, submit_reference
from dmd_gateway.services.registrations import X402_PAY_ACTION

router = APIRouter(prefix="/v1", tags=["payments"])

ID_PATTERN = r"^dmd_payment_intent_[0-9A-HJKMNP-TV-Z]{26}$"
PAYMENT_PREFIX = "dmd_payment_intent_"
REGISTRATION_PREFIX = "dmd_did_intent_"
PAYMENT_ID_HELP = "`payment_intent_id` from the quote (it starts with `dmd_payment_intent_`)."


def unknown_payment(session: Session, payment_intent_id: str) -> ApiError:
    """404 for an unknown payment ID, naming the ID that was probably meant.

    Both IDs of a quote look alike, so a registration ID with its start changed is a common
    slip. The payment ID named here is also shown by the registration's own status.
    """
    suffix = payment_intent_id.removeprefix(PAYMENT_PREFIX)
    meant = session.scalars(
        select(PaymentIntent.id).where(
            PaymentIntent.registration_intent_id == REGISTRATION_PREFIX + suffix
        )
    ).first()
    if meant is not None:
        return ApiError(
            404,
            "not_found",
            f"Unknown payment intent. {REGISTRATION_PREFIX}{suffix} is a registration ID with "
            f"the same ending; the payment_intent_id of that quote is {meant}.",
        )
    return ApiError(
        404,
        "not_found",
        "Unknown payment intent. Copy payment_intent_id exactly from the quote: it starts "
        "with dmd_payment_intent_, and after that start it differs from "
        "registration_intent_id.",
    )


class PaymentStatusResponse(BaseModel):
    payment_intent_id: str
    registration_intent_id: str | None  # a DID registration's payment
    purchase_id: str | None = None  # a donation's payment
    payment_option: str
    status: PaymentStatus
    asset: str
    network: str
    amount_due: str
    amount_received: str
    confirmations: int | None
    required_confirmations: int | None
    transactions: list[str]
    refund_due: str | None
    expires_at: datetime
    updated_at: datetime
    next_action: str


@router.get(
    "/payments/{payment_intent_id}",
    response_model=PaymentStatusResponse,
    summary="Payment status: detection, confirmations, settlement, refunds",
    responses={404: {"description": "Unknown payment intent."}},
)
def payment_status(
    request: Request,
    payment_intent_id: str = Path(pattern=ID_PATTERN, description=PAYMENT_ID_HELP),
) -> PaymentStatusResponse:
    with transaction(request.app.state.session_factory) as session:
        intent = session.get(PaymentIntent, payment_intent_id)
        if intent is None:
            raise unknown_payment(session, payment_intent_id)
        last = session.scalars(
            select(PaymentEvent)
            .where(PaymentEvent.payment_intent_id == intent.id)
            .order_by(PaymentEvent.id.desc())
            .limit(1)
        ).first()
        detail = (last.detail or {}) if last else {}
        decimals = intent.asset_decimals
        received = int(last.observed_amount_base_units or 0) if last else 0
        refund = int(detail.get("refund_due_base_units", 0))
        status = PaymentStatus(intent.status)
        next_action = NEXT_ACTIONS[status]
        if intent.purchase_id is not None:
            # Donations are paid in one call: there is nothing to send, only to wait.
            next_action = "read_donation_status"
        elif intent.adapter == X402_ADAPTER and status is PaymentStatus.AWAITING_PAYMENT:
            next_action = X402_PAY_ACTION  # paid in answer to POST /v1/dids/registrations
        return PaymentStatusResponse(
            payment_intent_id=intent.id,
            registration_intent_id=intent.registration_intent_id,
            purchase_id=intent.purchase_id,
            payment_option=intent.rail_id,
            status=status,
            asset=intent.asset_symbol,
            network=intent.network,
            amount_due=format_base_units(int(intent.amount_base_units), decimals),
            amount_received=format_base_units(received, decimals),
            confirmations=last.confirmations if last else None,
            required_confirmations=intent.required_confirmations,
            transactions=list(detail.get("transactions", [])),
            refund_due=format_base_units(refund, decimals) if refund else None,
            expires_at=intent.quote_expires_at,
            updated_at=intent.updated_at,
            next_action=next_action,
        )


class ReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    transaction_reference: str = Field(
        min_length=8,
        max_length=128,
        description="Transaction ID / hash of the broadcast payment. Used only as a hint.",
    )


class ReferenceResponse(BaseModel):
    payment_intent_id: str
    transaction_reference: str
    status: str
    note: str


@router.post(
    "/payments/{payment_intent_id}/references",
    status_code=202,
    response_model=ReferenceResponse,
    summary="Tell the gateway the transaction reference of a broadcast payment (a hint only)",
    responses={
        200: {"description": "This reference was already recorded."},
        404: {"description": "Unknown payment intent."},
        409: {"description": "The payment is no longer open."},
        422: {"description": "Not a valid reference for the payment's network."},
        429: {"description": "Too many references for this payment."},
    },
)
def submit_payment_reference(
    body: ReferenceRequest,
    request: Request,
    payment_intent_id: str = Path(pattern=ID_PATTERN, description=PAYMENT_ID_HELP),
) -> JSONResponse:
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        submitted = submit_reference(session, payment_intent_id, body.transaction_reference, now)
    response = ReferenceResponse(
        payment_intent_id=submitted.payment_intent_id,
        transaction_reference=submitted.reference,
        status="recorded" if submitted.created else "already_recorded",
        note=(
            "A reference is only a hint: the payment is verified independently on chain "
            "before it counts."
        ),
    )
    return JSONResponse(
        status_code=202 if submitted.created else 200, content=response.model_dump(mode="json")
    )
