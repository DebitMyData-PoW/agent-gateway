"""Issued identities, credentials, entitlements and DID documents.

GET /v1/dids/{identity_id}             identity record
GET /v1/credentials/{credential_id}    signed registration credential
GET /v1/entitlements/{identity_id}     entitlements of an identity
GET /agents/{id}/did.json              DID document (did:web resolution)
GET /.well-known/did.json              issuer DID document (credential verification key)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
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
from dmd_gateway.services.registrations import (
    IssuanceContext,
    active_entitlements,
    credential_url,
    identity_record_url,
)

router = APIRouter(tags=["identities"])

ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
IDENTITY_ID = rf"^dmd_identity_{ULID}$"
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
    did_document_url: str
    registration_intent_id: str
    credentials: list[str]
    entitlements: list[str]
    created_at: datetime


@router.get(
    "/v1/dids/{identity_id}",
    response_model=IdentityRecord,
    summary="An issued DebitMyData identity",
    responses={404: {"description": "Unknown identity."}},
)
def identity(request: Request, identity_id: str = Path(pattern=IDENTITY_ID)) -> IdentityRecord:
    base = _ctx(request).public_base_url
    with transaction(request.app.state.session_factory) as session:
        record = session.get(AgentIdentity, identity_id)
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
            did_document_url=did_document_url(base, record.id),
            registration_intent_id=record.registration_intent_id,
            credentials=[credential_url(base, cid) for cid in credentials],
            entitlements=active_entitlements(session, record.id),
            created_at=record.created_at,
        )


class CredentialRecord(BaseModel):
    credential_id: str
    status: str
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
        return CredentialRecord(
            credential_id=record.id,
            status="revoked" if record.revoked_at else "active",
            format=record.format,
            issued_at=record.issued_at,
            revoked_at=record.revoked_at,
            credential=record.payload,
        )


class EntitlementItem(BaseModel):
    entitlement: str
    status: str
    granted_at: datetime
    source_credential_id: str | None


class Entitlements(BaseModel):
    subject_id: str
    did: str
    entitlements: list[EntitlementItem]


@router.get(
    "/v1/entitlements/{identity_id}",
    response_model=Entitlements,
    summary="Entitlements granted to an identity",
    responses={404: {"description": "Unknown identity."}},
)
def entitlements(request: Request, identity_id: str = Path(pattern=IDENTITY_ID)) -> Entitlements:
    with transaction(request.app.state.session_factory) as session:
        record = session.get(AgentIdentity, identity_id)
        if record is None:
            raise ApiError(404, "not_found", "Unknown identity.")
        rows = session.scalars(
            select(Entitlement)
            .where(Entitlement.subject_id == record.id)
            .order_by(Entitlement.entitlement)
        )
        return Entitlements(
            subject_id=record.id,
            did=record.public_identifier,
            entitlements=[
                EntitlementItem(
                    entitlement=row.entitlement,
                    status=row.status,
                    granted_at=row.granted_at,
                    source_credential_id=row.source_credential_id,
                )
                for row in rows
            ],
        )


@router.get(
    "/agents/{suffix}/did.json",
    summary="DID document of an issued identity (did:web)",
    responses={404: {"description": "Unknown DID."}, 410: {"description": "DID deactivated."}},
)
def agent_document(request: Request, suffix: str = Path(pattern=rf"^{ULID}$")) -> JSONResponse:
    base = _ctx(request).public_base_url
    with transaction(request.app.state.session_factory) as session:
        record = session.get(AgentIdentity, f"dmd_identity_{suffix}")
        if record is None:
            raise ApiError(404, "not_found", "Unknown DID.")
        if record.status != "active":
            raise ApiError(410, "deactivated", f"This DID is {record.status}.")
        controller = parse_controller_key(record.controller_public_key or "")
        document = agent_did_document(
            record.public_identifier,
            controller,
            record_url=identity_record_url(base, record.id),
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
        content=issuer_did_document(issuer.did, issuer.public_key_multibase),
        media_type=DID_JSON,
    )
