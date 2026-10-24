"""DID registration: controller challenges and proofs, issuance and registration status.

    POST /v1/dids/registrations/{registration_intent_id}/controller-challenges
    POST /v1/dids/registrations/{registration_intent_id}/controller-proofs
    POST /v1/dids/registrations                       (issue the DID)
    GET  /v1/dids/registrations/{registration_intent_id}

The registration intent ID is random and only known to whoever requested the quote.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dmd_gateway.db.models import RegistrationIntent
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import ApiError, error_body
from dmd_gateway.identity.did import did_document_url
from dmd_gateway.services import controller_proofs
from dmd_gateway.services.quotes import DID_REGISTRATION
from dmd_gateway.services.registrations import (
    IssuanceContext,
    IssuedIdentity,
    complete_registration,
    credential_url,
    evaluate,
    identity_record_url,
)
from dmd_gateway.services.x402_payments import (
    PAYMENT_RESPONSE,
    PAYMENT_SIGNATURE,
    RETRY_AFTER_SECONDS,
    X402Context,
    encode_header,
    register_paid,
    resource_info,
)

router = APIRouter(prefix="/v1", tags=["registration"])

REGISTRATION_ID = r"^dmd_did_intent_[0-9A-HJKMNP-TV-Z]{26}$"
CHALLENGE_ID = r"^dmd_challenge_[0-9A-HJKMNP-TV-Z]{26}$"
REGISTRATION_ID_HELP = "`registration_intent_id` from the quote (it starts with `dmd_did_intent_`)."


def _ctx(request: Request) -> IssuanceContext:
    return request.app.state.issuance


# --------------------------------------------------------------------------- challenges


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    controller_type: str = Field(
        default="ed25519",
        pattern=r"^ed25519$",
        description="Kind of controller to prove. Only ed25519 public keys for now.",
    )


class ChallengeResponse(BaseModel):
    challenge_id: str
    registration_intent_id: str
    controller: str
    message: str
    signature_algorithm: str
    signature_encoding: str
    expires_at: datetime
    proof_url: str


@router.post(
    "/dids/registrations/{registration_intent_id}/controller-challenges",
    status_code=201,
    response_model=ChallengeResponse,
    summary="Get a one-time message to sign with the controller key",
    responses={
        404: {"description": "Unknown registration intent."},
        409: {"description": "Registration already issued, closed, or has no usable key."},
        429: {"description": "Too many open challenges."},
    },
)
def create_challenge(
    request: Request,
    registration_intent_id: str = Path(pattern=REGISTRATION_ID, description=REGISTRATION_ID_HELP),
    body: Annotated[ChallengeRequest | None, Body()] = None,
) -> ChallengeResponse:
    settings = request.app.state.settings
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        challenge = controller_proofs.create_challenge(
            session,
            registration_intent_id,
            audience=settings.public_base_url,
            ttl=timedelta(seconds=settings.controller_challenge_ttl_seconds),
            now=now,
        )
        return ChallengeResponse(
            challenge_id=challenge.id,
            registration_intent_id=registration_intent_id,
            controller=challenge.controller_value,
            message=challenge.message,
            signature_algorithm="ed25519",
            signature_encoding="base64url (base64 and hex are also accepted)",
            expires_at=challenge.expires_at,
            proof_url=(
                f"{settings.public_base_url}/v1/dids/registrations/"
                f"{registration_intent_id}/controller-proofs"
            ),
        )


# --------------------------------------------------------------------------- proofs


class ProofRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [{"challenge_id": "dmd_challenge_...", "signature": "<base64url>"}]
        },
    )

    challenge_id: str = Field(
        pattern=CHALLENGE_ID, description="`challenge_id` from the controller challenge."
    )
    signature: str = Field(
        min_length=86,
        max_length=128,
        description="Ed25519 signature of the challenge message (UTF-8), base64url/base64/hex: "
        "about 86 characters. Not the x402 payment (the long value starting with eyJ).",
    )

    @field_validator("signature", mode="before")
    @classmethod
    def _not_a_payment(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().startswith("eyJ") and len(value) > 128:
            raise ValueError(
                "this is an x402 payment (payment_signature), which goes in the "
                "PAYMENT-SIGNATURE header of POST /v1/dids/registrations; here, send the "
                "Ed25519 signature of the challenge message"
            )
        return value


class ProofResponse(BaseModel):
    proof_id: str
    registration_intent_id: str
    status: str
    verified_at: datetime | None
    next_action: str


@router.post(
    "/dids/registrations/{registration_intent_id}/controller-proofs",
    status_code=201,
    response_model=ProofResponse,
    summary="Prove control of the controller key by returning the signed challenge",
    responses={
        404: {"description": "Unknown registration or challenge."},
        409: {"description": "Challenge already answered, or registration closed."},
        410: {"description": "Challenge expired."},
        422: {"description": "Wrong or malformed signature (a wrong one uses up the challenge)."},
    },
)
def submit_proof(
    body: ProofRequest,
    request: Request,
    registration_intent_id: str = Path(pattern=REGISTRATION_ID, description=REGISTRATION_ID_HELP),
) -> Any:
    ctx = _ctx(request)
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        outcome = controller_proofs.submit_proof(
            session,
            registration_intent_id,
            challenge_id=body.challenge_id,
            signature=body.signature,
            now=now,
        )
        session.flush()
        registration = session.get(RegistrationIntent, registration_intent_id)
        assert registration is not None
        state = evaluate(session, ctx, registration)
        response = ProofResponse(
            proof_id=outcome.proof.id,
            registration_intent_id=registration_intent_id,
            status=outcome.proof.status,
            verified_at=outcome.proof.verified_at,
            next_action=state.next_action if outcome.verified else "request_new_challenge",
        )
    # The rejected proof is recorded (committed above) before the error is returned.
    if not outcome.verified:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_signature",
                "The signature does not match the controller public key. This challenge is "
                "used up; request a new one.",
                {"proof_id": response.proof_id, "next_action": "request_new_challenge"},
            ),
        )
    return JSONResponse(status_code=201, content=response.model_dump(mode="json"))


# --------------------------------------------------------------------------- issuance


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"examples": [{"registration_intent_id": "dmd_did_intent_..."}]},
    )

    registration_intent_id: str = Field(pattern=REGISTRATION_ID, description=REGISTRATION_ID_HELP)


class IssuedRegistration(BaseModel):
    registration_intent_id: str
    status: str
    identity_id: str
    did: str
    did_document_url: str
    identity_url: str
    credential_id: str
    credential_url: str
    credential: dict[str, Any]
    entitlements: list[str]
    issued_at: datetime
    payment_response: dict[str, Any] | None = Field(
        default=None,
        description="x402 payments: the settlement (also in the PAYMENT-RESPONSE header).",
    )


def _issued_response(
    base_url: str, issued: IssuedIdentity, payment_response: dict[str, Any] | None = None
) -> IssuedRegistration:
    identity, credential = issued.identity, issued.credential
    return IssuedRegistration(
        registration_intent_id=identity.registration_intent_id,
        status="issued",
        identity_id=identity.id,
        did=identity.public_identifier,
        did_document_url=did_document_url(base_url, identity.id),
        identity_url=identity_record_url(base_url, identity.id),
        credential_id=credential.id,
        credential_url=credential_url(base_url, credential.id),
        credential=credential.payload,
        entitlements=issued.entitlements,
        issued_at=credential.issued_at,
        payment_response=payment_response,
    )


def _x402_context(request: Request) -> X402Context:
    state = request.app.state
    service = state.registry.services[DID_REGISTRATION]
    return X402Context(
        factory=state.session_factory,
        adapters=state.quote_context.adapters,
        issuance=state.issuance,
        resource=resource_info(state.settings.public_base_url, service.path, service.title),
        settlement_wait=timedelta(seconds=state.settings.x402_settlement_wait_seconds),
    )


@router.post(
    "/dids/registrations",
    status_code=201,
    response_model=IssuedRegistration,
    summary="Issue the DID once the payment is settled and the controller proof is verified",
    description=(
        "For a quote paid in DMD, call this once the payment is settled and the controller "
        "proof verified. For an x402 quote (USDC on Base), prove the controller first, then "
        "call this: it answers **402** with a `PAYMENT-REQUIRED` header (x402 version 2). "
        "Call it again with your signed payment in the `PAYMENT-SIGNATURE` header; the "
        "payment is settled, checked on chain, and the DID is issued in the same answer "
        "(`PAYMENT-RESPONSE` header), or 202 if confirmations take longer."
    ),
    responses={
        200: {"description": "Already issued: the same DID and credential are returned."},
        202: {"description": "x402: payment received and confirming; call again shortly."},
        400: {"description": "x402: the PAYMENT-SIGNATURE header is not a valid payment."},
        402: {"description": "x402: payment required, or the payment was refused."},
        404: {"description": "Unknown registration intent."},
        409: {"description": "Not ready: the response lists what is still missing."},
        503: {"description": "DID issuance or the payment service is not available."},
    },
)
def register(
    body: RegistrationRequest,
    request: Request,
    payment_signature: Annotated[
        str | None,
        Header(
            alias=PAYMENT_SIGNATURE,
            description="x402 only: the base64 PaymentPayload answering the 402 (a long "
            "value starting with eyJ; on staging, payment_signature from "
            "POST /v1/staging/test-payments). Not the controller-proof signature.",
        ),
    ] = None,
) -> JSONResponse:
    ctx = _ctx(request)
    paid = register_paid(_x402_context(request), body.registration_intent_id, payment_signature)
    if paid is not None:
        headers = {}
        if paid.payment_response:
            headers[PAYMENT_RESPONSE] = encode_header(paid.payment_response)
        if paid.issued is None:
            headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
            return JSONResponse(status_code=202, content=paid.body, headers=headers)
        payload = _issued_response(
            ctx.public_base_url, paid.issued, paid.payment_response
        ).model_dump(mode="json")
        return JSONResponse(status_code=paid.status_code, content=payload, headers=headers)

    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        issued = complete_registration(session, ctx, body.registration_intent_id, now)
        session.flush()
        payload = _issued_response(ctx.public_base_url, issued).model_dump(mode="json")
    return JSONResponse(status_code=201 if issued.created else 200, content=payload)


# --------------------------------------------------------------------------- status


class ControllerStatus(BaseModel):
    public_key: str | None
    wallet_address: str | None
    proof_status: str


class PaymentSummary(BaseModel):
    payment_intent_id: str
    payment_option: str
    status: str


class RegistrationStatus(BaseModel):
    registration_intent_id: str
    status: str
    display_name: str
    participant_type: str
    controller: ControllerStatus
    payment: PaymentSummary | None
    blockers: list[str]
    next_action: str
    identity_id: str | None
    did: str | None
    expires_at: datetime


@router.get(
    "/dids/registrations/{registration_intent_id}",
    response_model=RegistrationStatus,
    summary="Registration progress: payment, controller proof, what is still missing",
    responses={404: {"description": "Unknown registration intent."}},
)
def registration_status(
    request: Request,
    registration_intent_id: str = Path(pattern=REGISTRATION_ID, description=REGISTRATION_ID_HELP),
) -> RegistrationStatus:
    ctx = _ctx(request)
    with transaction(request.app.state.session_factory) as session:
        registration = session.get(RegistrationIntent, registration_intent_id)
        if registration is None:
            raise ApiError(404, "not_found", "Unknown registration intent.")
        state = evaluate(session, ctx, registration)
        blockers = list(state.blockers)
        if not ctx.available and state.identity is None:
            blockers.append("DID issuance is not enabled on this gateway yet")
        return RegistrationStatus(
            registration_intent_id=registration.id,
            status=registration.status,
            display_name=registration.display_name,
            participant_type=registration.participant_type,
            controller=ControllerStatus(
                public_key=registration.controller_public_key,
                wallet_address=registration.controller_wallet_address,
                proof_status=state.proof_status.value,
            ),
            payment=(
                PaymentSummary(
                    payment_intent_id=state.payment.id,
                    payment_option=state.payment.rail_id,
                    status=state.payment.status,
                )
                if state.payment
                else None
            ),
            blockers=blockers,
            next_action=state.next_action,
            identity_id=state.identity.id if state.identity else None,
            did=state.identity.public_identifier if state.identity else None,
            expires_at=registration.expires_at,
        )
