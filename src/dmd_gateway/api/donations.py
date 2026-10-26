"""Donations (the inference donation): the donor chooses the amount and pays with x402."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import JSONResponse

from dmd_gateway.errors import ApiError
from dmd_gateway.services.donations import (
    PURCHASE_ID_PATTERN,
    DonationContext,
    DonationRequest,
    donate,
    donation_status,
)
from dmd_gateway.services.x402_payments import (
    PAYMENT_RESPONSE,
    PAYMENT_SIGNATURE,
    RETRY_AFTER_SECONDS,
    encode_header,
)

router = APIRouter(prefix="/v1", tags=["donations"])


def donation_context(request: Request) -> DonationContext:
    state = request.app.state
    return DonationContext(
        factory=state.session_factory,
        adapters=state.quote_context.adapters,
        issuance=state.issuance,
        settlement_wait=timedelta(seconds=state.settings.x402_settlement_wait_seconds),
    )


@router.post(
    "/donations/{name}",
    summary="Donate (inference donation), paid with x402",
    description=(
        "A voluntary donation towards the AI inference behind DebitMyData's agentic services, "
        "in an amount you choose (`amount_usd`), paid in USDC on Base through x402. Call it "
        "with the amount: it answers **402** with a `PAYMENT-REQUIRED` header. Call it again "
        "with the same body and your signed payment in the `PAYMENT-SIGNATURE` header: the "
        "payment is settled, checked on chain, and the answer carries a receipt signed by "
        "DebitMyData (or 202 while confirmations take longer; then read `status_url`). "
        "Sending the same signed payment again returns the same donation. A call without "
        "`amount_usd` answers 402 with example terms (for x402 tools) and cannot be paid."
    ),
    responses={
        200: {"description": "This signed payment was already received: the same donation."},
        201: {"description": "Paid and settled: the donation and its signed receipt."},
        202: {"description": "Payment received and confirming; read status_url shortly."},
        400: {"description": "The PAYMENT-SIGNATURE header is not a valid x402 payment."},
        402: {"description": "Payment required, or the payment was refused."},
        404: {"description": "Unknown donation."},
        409: {"description": "The authorization was already used, or the payment failed."},
        422: {"description": "amount_usd is outside the accepted range."},
        503: {"description": "Donations are not open on this gateway yet."},
    },
)
def make_donation(
    request: Request,
    name: Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{1,40}$")],
    body: DonationRequest | None = None,
    payment_signature: Annotated[
        str | None,
        Header(
            alias=PAYMENT_SIGNATURE,
            description="The base64 x402 PaymentPayload answering the 402.",
        ),
    ] = None,
) -> JSONResponse:
    registry = request.app.state.registry
    path = f"/v1/donations/{name}"
    donation_id = next((d for d, c in registry.donations.items() if c.path == path), None)
    if donation_id is None:
        raise ApiError(404, "not_found", "Unknown donation.")
    result = donate(donation_context(request), donation_id, body, payment_signature)
    headers: dict[str, str] = {}
    if result.payment_response:
        headers[PAYMENT_RESPONSE] = encode_header(result.payment_response)
    if result.status_code == 202:
        headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


@router.get(
    "/donations/{purchase_id}",
    summary="A donation: payment status and, once settled, its signed receipt",
    responses={404: {"description": "Unknown donation."}},
)
def get_donation(
    request: Request,
    purchase_id: Annotated[
        str,
        Path(pattern=PURCHASE_ID_PATTERN, description="purchase_id (starts with dmd_purchase_)"),
    ],
) -> JSONResponse:
    return JSONResponse(content=donation_status(donation_context(request), purchase_id))
