"""POST /v1/dids/registration-quotes (QUOTE-05)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from dmd_gateway.db.session import transaction
from dmd_gateway.services import idempotency
from dmd_gateway.services.quotes import (
    DID_REGISTRATION,
    QuoteContext,
    QuoteRequest,
    RegistrationQuote,
    create_registration_quote,
    resolve_payment_option,
)

router = APIRouter(prefix="/v1", tags=["registration"])

IDEMPOTENCY_SCOPE = "dids.registration_quotes"

_ERRORS = {
    409: {"description": "The chosen payment option is not currently available."},
    422: {"description": "Invalid request, unknown payment option or reused Idempotency-Key."},
    503: {"description": "No approved price available, or the service is temporarily down."},
}


@router.post(
    "/dids/registration-quotes",
    status_code=201,
    response_model=RegistrationQuote,
    summary="Get a time-limited quote for a DebitMyData DID registration",
    responses={**_ERRORS},
)
def create_quote(
    body: QuoteRequest,
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            pattern=idempotency.KEY_PATTERN,
            description="Optional random key (e.g. a UUID). Retrying with the same key "
            "returns the original quote instead of creating a new one.",
        ),
    ] = None,
) -> JSONResponse:
    ctx: QuoteContext = request.app.state.quote_context
    # Refuse unavailable options before touching the database.
    resolve_payment_option(ctx, DID_REGISTRATION, body.preferred_payment_option)

    now = datetime.now(UTC)
    request_hash = idempotency.fingerprint(body.model_dump(mode="json"))
    with transaction(request.app.state.session_factory) as session:
        key_row = None
        if idempotency_key:
            key_row, replay = idempotency.claim(
                session, IDEMPOTENCY_SCOPE, idempotency_key, request_hash
            )
            if replay is not None:
                status, stored = replay
                return JSONResponse(
                    status_code=status, content=stored, headers={"Idempotent-Replayed": "true"}
                )

        quote = create_registration_quote(session, ctx, body, now)
        payload = quote.model_dump(mode="json")
        if key_row is not None:
            idempotency.complete(key_row, 201, payload, now)

    return JSONResponse(status_code=201, content=payload)
