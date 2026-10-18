"""GET /v1/payments/{payment_intent_id}: payment status for the paying agent.

Returns the normalized status plus confirmation, settlement and refund details
(directive MCP tool ``dmd_get_payment_status``). The payment-intent ID is random and only
known to whoever requested the quote.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel
from sqlalchemy import select

from dmd_gateway.db.models import PaymentEvent, PaymentIntent
from dmd_gateway.db.session import transaction
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.errors import ApiError

router = APIRouter(prefix="/v1", tags=["payments"])

S = PaymentStatus
ID_PATTERN = r"^dmd_payment_intent_[0-9A-HJKMNP-TV-Z]{26}$"

NEXT_ACTIONS: dict[PaymentStatus, str] = {
    S.CREATED: "send_payment",
    S.AWAITING_PAYMENT: "send_payment",
    S.DETECTED: "wait_for_confirmations",
    S.CONFIRMING: "wait_for_confirmations",
    S.VERIFIED: "wait_for_confirmations",
    S.OVERPAID: "wait_for_confirmations",
    S.REORGED: "wait_for_confirmations",
    S.SETTLED: "prove_wallet_control",
    S.EXPIRED: "request_new_quote",
    S.FAILED: "request_new_quote",
    S.UNDERPAID: "contact_support_for_refund",
    S.REFUNDED: "request_new_quote",
    S.REVERSED: "contact_support",
    S.DISPUTED: "contact_support",
}


class PaymentStatusResponse(BaseModel):
    payment_intent_id: str
    registration_intent_id: str
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
    payment_intent_id: str = Path(pattern=ID_PATTERN),
) -> PaymentStatusResponse:
    with transaction(request.app.state.session_factory) as session:
        intent = session.get(PaymentIntent, payment_intent_id)
        if intent is None:
            raise ApiError(404, "not_found", "Unknown payment intent.")
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
        return PaymentStatusResponse(
            payment_intent_id=intent.id,
            registration_intent_id=intent.registration_intent_id,
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
            next_action=NEXT_ACTIONS[status],
        )
