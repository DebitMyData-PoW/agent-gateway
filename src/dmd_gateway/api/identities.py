"""Issued identities, credentials, entitlements and DID-LLM documents.

GET /v1/dids/{identity_id}             identity record
GET /v1/credentials/{credential_id}    signed registration credential
GET /v1/entitlements/{identity_id}     entitlements of an identity (such as BottyBotty
                                       protection, with the end of the period bought)
POST /v1/dids/verifications            verify an agent by its DID-LLM: its status and signed
                                       credential, a challenge, then whether the agent's
                                       signature of it is the owner's (services/verification.py)

{identity_id} is the identity ID (dmd_identity_...) or the DID-LLM itself.
GET /agents/{id}/did.json              DID-LLM document (did:web resolution)
GET /.well-known/did.json              issuer DID document (credential verification key)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from dmd_gateway.db.models import AgentIdentity, Credential, Entitlement
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import parse_controller_key
from dmd_gateway.identity.did import (
    agent_did_document,
    did_document_url,
    issuer_did_document,
)
from dmd_gateway.identity.wallets import Wallet
from dmd_gateway.services.identity_status import IDENTITY_OR_DID, public_identity
from dmd_gateway.services.registrations import (
    IssuanceContext,
    active_entitlements,
    credential_url,
    identity_record_url,
)
from dmd_gateway.services.verification import verify_agent

router = APIRouter(tags=["identities"])

ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
IDENTITY_HELP = (
    "The identity ID (`dmd_identity_...`) or the DID-LLM itself "
    "(`did:web:api.debitmydata.io:agents:...`)."
)
CREDENTIAL_ID = rf"^dmd_credential_{ULID}$"
DID_JSON = "application/did+json"


def _ctx(request: Request) -> IssuanceContext:
    return request.app.state.issuance


class IdentityRecord(BaseModel):
    identity_id: str
    did: str
    status: str
    display_name: str
    participant_type: str
    controller_public_key: str | None
    controller_wallet: str | None = Field(
        default=None, description="The controller wallet, as `eip155:<chain id>:0x...`"
    )
    did_document_url: str
    registration_intent_id: str
    credentials: list[str]
    entitlements: list[str]
    created_at: datetime
    owner_changed_at: datetime | None = Field(
        default=None,
        description="When its owners last changed (an owner change signed by its owners); "
        "null if never.",
    )


@router.get(
    "/v1/dids/{identity_id}",
    response_model=IdentityRecord,
    summary="An issued DebitMyData identity",
    responses={404: {"description": "Unknown identity."}},
)
def identity(
    request: Request,
    identity_id: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
) -> IdentityRecord:
    base = _ctx(request).public_base_url
    with transaction(request.app.state.session_factory) as session:
        record = public_identity(session, identity_id)
        if record is None:
            raise ApiError(404, "not_found", "Unknown identity.")
        credentials = session.scalars(
            select(Credential.id)
            .where(Credential.identity_id == record.id)
            .order_by(Credential.issued_at)
        )
        return IdentityRecord(
            identity_id=record.id,
            did=record.public_identifier,
            status=record.status,
            display_name=record.display_name,
            participant_type=record.participant_type,
            controller_public_key=record.controller_public_key,
            controller_wallet=(
                f"{record.controller_network}:{record.controller_wallet_address}"
                if record.controller_wallet_address
                else None
            ),
            did_document_url=did_document_url(base, record.id),
            registration_intent_id=record.registration_intent_id,
            credentials=[credential_url(base, cid) for cid in credentials],
            entitlements=active_entitlements(session, record.id),
            created_at=record.created_at,
            owner_changed_at=record.controller_changed_at,
        )


class VerifiedCredential(BaseModel):
    credential_id: str
    url: str
    status: str = Field(description="active, suspended (with its DID-LLM) or revoked")
    issued_at: datetime
    signature_valid: bool = Field(description="Signed with DebitMyData's issuer key")


class Owner(BaseModel):
    type: str = Field(description="key (Ed25519) or wallet (EVM)")
    value: str


class ProofResult(BaseModel):
    signature_valid: bool
    signed_with: str | None = Field(default=None, description="key or wallet")
    method: str | None = None
    audience: str | None = Field(default=None, description="The challenge's To: line")
    nonce: str | None = Field(
        default=None, description="The challenge's nonce: check it is the one you were given"
    )
    expires_at: datetime | None = None
    reason: str | None = None


class Challenge(BaseModel):
    message: str = Field(description="What the agent signs, exactly")
    nonce: str
    expires_at: datetime
    next: str


class VerificationRequest(BaseModel):
    did: str = Field(min_length=1, max_length=300, description="The DID-LLM to verify")
    audience: str | None = Field(
        default=None,
        max_length=300,
        description="Who verifies (your name or site), written in the challenge",
    )
    message: str | None = Field(
        default=None, max_length=2000, description="The challenge message the agent signed"
    )
    signature: str | None = Field(
        default=None,
        max_length=20000,
        description="The agent's signature of the message: its DID-LLM key's, or 0x... from "
        "its wallet",
    )


class Verification(BaseModel):
    did: str
    verified: bool = Field(
        description="Issued by DebitMyData, active, its credential signed by DebitMyData and, "
        "when a signature was sent, signed by its owner"
    )
    found: bool
    reasons: list[str] = Field(description="Why it is not verified (empty when it is)")
    checked_at: datetime
    status: str | None = None
    display_name: str | None = None
    participant_type: str | None = None
    issued_at: datetime | None = None
    issuer: str | None = None
    credential: VerifiedCredential | None = None
    controllers: list[Owner] = []
    entitlements: list[str] = []
    proof: ProofResult | None = None
    challenge: Challenge | None = Field(
        default=None, description="Without a signature: the message for the agent to sign"
    )


@router.post(
    "/v1/dids/verifications",
    response_model=Verification,
    response_model_exclude_none=True,
    summary="Verify an agent by its DID-LLM (and that it controls it)",
)
def verify(request: Request, body: VerificationRequest) -> Verification:
    """Send `did` (and `audience`, your name) for the DID-LLM's status, its signed credential
    checked against DebitMyData's issuer key, and a `challenge`. Have the agent sign the
    challenge's message with its DID-LLM's key or wallet, then send `did`, `message` and
    `signature`: `proof` says whether the signature is the owner's. Check that `proof.nonce` is
    the nonce you were given."""
    ctx = _ctx(request)
    with transaction(request.app.state.session_factory) as session:
        result = verify_agent(
            session,
            body.did.strip(),
            issuer=ctx.issuer,
            base=ctx.public_base_url,
            wallets=request.app.state.wallet_verifier,
            now=datetime.now(UTC),
            audience=body.audience,
            message=body.message,
            signature=body.signature,
        )
    return Verification(**result)


class CredentialRecord(BaseModel):
    credential_id: str
    status: str = Field(description="active, suspended (with its DID-LLM) or revoked")
    format: str
    issued_at: datetime
    revoked_at: datetime | None
    credential: dict[str, Any]


@router.get(
    "/v1/credentials/{credential_id}",
    response_model=CredentialRecord,
    summary="A signed registration credential (verify it with the issuer DID document)",
    responses={404: {"description": "Unknown credential."}},
)
def credential(
    request: Request, credential_id: str = Path(pattern=CREDENTIAL_ID)
) -> CredentialRecord:
    with transaction(request.app.state.session_factory) as session:
        record = session.get(Credential, credential_id)
        if record is None:
            raise ApiError(404, "not_found", "Unknown credential.")
        identity = session.get(AgentIdentity, record.identity_id)
        status = "active"
        if record.revoked_at:
            status = "revoked"
        elif identity is not None and identity.status != "active":
            # A suspended DID's credential is suspended with it (ID-13).
            status = identity.status
        return CredentialRecord(
            credential_id=record.id,
            status=status,
            format=record.format,
            issued_at=record.issued_at,
            revoked_at=record.revoked_at,
            credential=record.payload,
        )


class EntitlementItem(BaseModel):
    entitlement: str
    status: str = Field(description="active, expired, suspended or revoked")
    granted_at: datetime
    expires_at: datetime | None = Field(
        default=None, description="End of the period bought (null: no end)."
    )
    source_credential_id: str | None


class Entitlements(BaseModel):
    subject_id: str
    did: str
    entitlements: list[EntitlementItem]


@router.get(
    "/v1/entitlements/{identity_id}",
    response_model=Entitlements,
    summary="Entitlements of a DID-LLM, such as its BottyBotty tier (and any end of period)",
    responses={404: {"description": "Unknown identity."}},
)
def entitlements(
    request: Request,
    identity_id: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
) -> Entitlements:
    with transaction(request.app.state.session_factory) as session:
        record = public_identity(session, identity_id)
        if record is None:
            raise ApiError(404, "not_found", "Unknown identity.")
        rows = session.scalars(
            select(Entitlement)
            .where(Entitlement.subject_id == record.id)
            .order_by(Entitlement.entitlement)
        )
        now = datetime.now(UTC)
        return Entitlements(
            subject_id=record.id,
            did=record.public_identifier,
            entitlements=[
                EntitlementItem(
                    entitlement=row.entitlement,
                    status=(
                        "expired"
                        if row.status == "active" and row.expires_at and row.expires_at <= now
                        else row.status
                    ),
                    granted_at=row.granted_at,
                    expires_at=row.expires_at,
                    source_credential_id=row.source_credential_id,
                )
                for row in rows
            ],
        )


@router.get(
    "/agents/{suffix}/did.json",
    summary="DID-LLM document of an issued identity (did:web)",
    responses={
        404: {"description": "Unknown DID-LLM."},
        410: {"description": "DID-LLM deactivated."},
    },
)
def agent_document(request: Request, suffix: str = Path(pattern=rf"^{ULID}$")) -> JSONResponse:
    base = _ctx(request).public_base_url
    with transaction(request.app.state.session_factory) as session:
        record = session.get(AgentIdentity, f"dmd_identity_{suffix}")
        if record is None:
            raise ApiError(404, "not_found", "Unknown DID-LLM.")
        if record.status != "active":
            raise ApiError(410, "deactivated", f"This DID-LLM is {record.status}.")
        controller = (
            parse_controller_key(record.controller_public_key)
            if record.controller_public_key
            else None
        )
        wallet = (
            Wallet(record.controller_network or "", record.controller_wallet_address)
            if record.controller_wallet_address
            else None
        )
        document = agent_did_document(
            record.public_identifier,
            controller,
            record_url=identity_record_url(base, record.id),
            wallet=wallet,
        )
    return JSONResponse(content=document, media_type=DID_JSON)


@router.get(
    "/.well-known/did.json",
    summary="Issuer DID document: the public key that signs DebitMyData credentials",
    responses={404: {"description": "Credential issuance is not enabled."}},
)
def issuer_document(request: Request) -> JSONResponse:
    issuer = _ctx(request).issuer
    if issuer is None:
        raise ApiError(404, "not_found", "Credential issuance is not enabled on this gateway.")
    return JSONResponse(
        content=issuer_did_document(issuer.did, issuer.public_key_multibase, issuer.retired_keys),
        media_type=DID_JSON,
    )
