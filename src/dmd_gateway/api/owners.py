"""Owner changes and owner revocation of an issued DID-LLM (ID-12; services/owner_changes.py).

    POST /v1/dids/{identity}/owner-changes                         a change: the messages to sign
    GET  /v1/dids/{identity}/owner-changes/{change_id}             where it stands
    POST /v1/dids/{identity}/owner-changes/{change_id}/signatures  one signed message; the change
                                                                    applies with the last one
    POST /v1/dids/{identity}/revocations                           the message to revoke for good
    GET  /v1/dids/{identity}/revocations/{challenge_id}            that message again
    POST /v1/dids/{identity}/revocations/{challenge_id}/signature  the signed message: revoked

{identity} is the DID-LLM or its identity ID (dmd_identity_...). Nothing changes without the
signatures of the DID-LLM's owners: a current owner approves an owner change and each new owner
accepts it; one owner signs a revocation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from dmd_gateway.api.registrations import CHALLENGE_ID, SIGNING
from dmd_gateway.db.models import AgentIdentity, ControllerChallenge, OwnerChange
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import ApiError, error_body
from dmd_gateway.identity.crypto import ControllerKeyError, parse_controller_key
from dmd_gateway.identity.wallets import (
    WalletError,
    controller_networks,
    parse_wallet,
    split_account,
)
from dmd_gateway.services import owner_changes
from dmd_gateway.services.controller_proofs import ED25519, EIP155
from dmd_gateway.services.identity_status import IDENTITY_OR_DID, public_identity

router = APIRouter(prefix="/v1", tags=["owners"])

ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
CHANGE_ID = rf"^dmd_owner_change_{ULID}$"
IDENTITY_HELP = (
    "The DID-LLM (`did:web:api.debitmydata.io:agents:...`) or its identity ID (`dmd_identity_...`)."
)
OWNER_TYPES = {ED25519: "key", EIP155: "wallet"}


def _base(request: Request) -> str:
    return request.app.state.settings.public_base_url.rstrip("/")


def _owners(found: dict[str, str]) -> list[Owner]:
    return [Owner(type=OWNER_TYPES[kind], value=value) for kind, value in found.items()]


class Owner(BaseModel):
    type: Literal["key", "wallet"]
    value: str = Field(description="`ed25519:z6Mk...` or `eip155:<chain id>:0x...`")


# --------------------------------------------------------------------------- owner changes


class OwnerChangeRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {"new_controller_public_key": "ed25519:z6Mk<the new owner key>"},
                {
                    "new_controller_public_key": "ed25519:z6Mk<the key it keeps>",
                    "new_controller_wallet_address": "0x<the new owner wallet>",
                },
            ]
        },
    )

    new_controller_public_key: str | None = Field(
        default=None,
        description="The DID-LLM's owner key after the change (Ed25519, `ed25519:z6Mk...`): "
        "the current one to keep it, a new one to replace it, or left out to have none.",
    )
    new_controller_wallet_address: str | None = Field(
        default=None,
        description="Its owner wallet after the change (EVM, `0x...` or `eip155:8453:0x...`), "
        "the same way.",
    )
    new_controller_network: str | None = Field(
        default=None, description="The wallet's network, e.g. eip155:8453 for Base (default)."
    )

    @field_validator("new_controller_public_key")
    @classmethod
    def _valid_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return parse_controller_key(value).canonical
        except ControllerKeyError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("new_controller_wallet_address")
    @classmethod
    def _valid_wallet(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            network, address = split_account(value)
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        return f"{network}:{address}" if network else address

    @model_validator(mode="after")
    def _an_owner(self) -> OwnerChangeRequest:
        if not self.new_controller_public_key and not self.new_controller_wallet_address:
            raise ValueError(
                "name the owners after the change: new_controller_public_key and/or "
                "new_controller_wallet_address (a DID-LLM always has at least one)"
            )
        if self.new_controller_network and not self.new_controller_wallet_address:
            raise ValueError("new_controller_network goes with new_controller_wallet_address")
        return self


class ChangeMessage(BaseModel):
    challenge_id: str
    role: Literal["approve", "accept"] = Field(
        description="approve: a current owner hands the DID-LLM over (one approval is "
        "enough); accept: a new owner takes it (each new owner)."
    )
    signer: str = Field(description="The owner key or wallet that signs this message.")
    status: str = Field(description="open, verified, rejected or expired")
    message: str = Field(description="Sign exactly this text (UTF-8).")
    signature_algorithm: str
    signature_encoding: str


class WaitingFor(BaseModel):
    approval: bool = Field(description="True until one current owner has approved.")
    acceptances: list[str] = Field(description="The new owners that have not accepted yet.")


class OwnerChangeView(BaseModel):
    change_id: str
    did: str
    status: str = Field(
        description="open, applied, cancelled (another change applied first) or expired"
    )
    previous_owners: list[Owner]
    new_owners: list[Owner]
    waiting_for: WaitingFor
    messages: list[ChangeMessage]
    expires_at: datetime
    applied_at: datetime | None
    signatures_url: str = Field(description="POST each signed message here.")
    next: str


def change_view(session: Session, change: OwnerChange, base: str, now: datetime) -> OwnerChangeView:
    identity = session.get(AgentIdentity, change.identity_id)
    assert identity is not None
    did = identity.public_identifier
    status = owner_changes.change_status(change, now)
    needed = owner_changes.still_needed(session, change)
    items = [
        ChangeMessage(
            challenge_id=item.id,
            role=owner_changes.role(change, item.controller_type, item.controller_value),
            signer=item.controller_value,
            status=owner_changes.message_status(session, item, now),
            message=item.message,
            **SIGNING[item.controller_type],
        )
        for item in owner_changes.messages(session, change)
    ]
    if status == "applied":
        step = "Done: the DID-LLM has its new owners. Earlier sign-ins have ended."
    elif status != "open":
        step = f"This change is {status}: request a new one."
    else:
        wanted = []
        if needed["approval"]:
            wanted.append("one current owner signs its approve message")
        if needed["acceptances"]:
            wanted.append("each new owner signs its accept message")
        step = (
            "Sign: " + "; ".join(wanted) + ". POST each signature (with its challenge_id) to "
            "signatures_url before expires_at."
        )
    return OwnerChangeView(
        change_id=change.id,
        did=did,
        status=status,
        previous_owners=_owners(owner_changes.previous_owners(change)),
        new_owners=_owners(owner_changes.new_owners(change)),
        waiting_for=WaitingFor(approval=needed["approval"], acceptances=needed["acceptances"]),
        messages=items,
        expires_at=change.expires_at,
        applied_at=change.applied_at,
        signatures_url=f"{base}/v1/dids/{did}/owner-changes/{change.id}/signatures",
        next=step,
    )


def _requested_owners(request: Request, body: OwnerChangeRequest) -> owner_changes.Owners:
    wallet = None
    if body.new_controller_wallet_address:
        networks, default = controller_networks(request.app.state.registry)
        try:
            wallet = parse_wallet(
                body.new_controller_wallet_address,
                body.new_controller_network,
                allowed_networks=networks,
                default_network=default,
            )
        except WalletError as exc:
            raise ApiError(422, "invalid_controller_wallet", str(exc)) from exc
    return owner_changes.Owners(public_key=body.new_controller_public_key, wallet=wallet)


@router.post(
    "/dids/{identity}/owner-changes",
    status_code=201,
    response_model=OwnerChangeView,
    summary="Change a DID-LLM's owners: get the messages its current and new owners sign",
    responses={
        404: {"description": "Unknown DID-LLM."},
        409: {"description": "Not active, the same owners, or a name reserved for DebitMyData."},
        422: {"description": "A new owner key or wallet is not valid."},
    },
)
def create_change(
    body: OwnerChangeRequest,
    request: Request,
    identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
) -> OwnerChangeView:
    state = request.app.state
    now = datetime.now(UTC)
    with transaction(state.session_factory) as session:
        change = owner_changes.create_change(
            session,
            identity,
            new=_requested_owners(request, body),
            audience=state.settings.public_base_url,
            now=now,
            brand_owners=state.quote_context.brand_owners,
        )
        return change_view(session, change, _base(request), now)


@router.get(
    "/dids/{identity}/owner-changes/{change_id}",
    response_model=OwnerChangeView,
    summary="Where an owner change stands, with its messages",
    responses={404: {"description": "Unknown DID-LLM or owner change."}},
)
def get_change(
    request: Request,
    identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
    change_id: str = Path(pattern=CHANGE_ID),
) -> OwnerChangeView:
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        change = _change_of(session, identity, change_id)
        return change_view(session, change, _base(request), now)


def _change_of(session: Session, identity: str, change_id: str) -> OwnerChange:
    record = public_identity(session, identity)
    change = session.get(OwnerChange, change_id)
    if record is None or change is None or change.identity_id != record.id:
        raise ApiError(404, "unknown_owner_change", "Unknown owner change for this DID-LLM.")
    return change


class SignatureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    challenge_id: str = Field(pattern=CHALLENGE_ID, description="The message's `challenge_id`.")
    signature: str = Field(
        min_length=86,
        max_length=16_400,
        description="The signature of that message: Ed25519 (base64url, base64 or hex), or "
        "what the wallet's personal_sign returns (0x...).",
    )


@router.post(
    "/dids/{identity}/owner-changes/{change_id}/signatures",
    status_code=201,
    response_model=OwnerChangeView,
    summary="Send one signed owner change message; the change applies with the last one needed",
    responses={
        404: {"description": "Unknown DID-LLM, owner change or message."},
        409: {
            "description": "Message already answered, change applied or cancelled, or the "
            "DID-LLM is not active."
        },
        410: {"description": "The change or the message expired."},
        422: {"description": "Wrong or malformed signature (a wrong one uses up the message)."},
        503: {"description": "A smart wallet's network cannot be asked right now (try again)."},
    },
)
def submit_signature(
    body: SignatureRequest,
    request: Request,
    identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
    change_id: str = Path(pattern=CHANGE_ID),
) -> Any:
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        outcome = owner_changes.submit_signature(
            session,
            identity,
            change_id,
            challenge_id=body.challenge_id,
            signature=body.signature,
            now=now,
            wallets=request.app.state.wallet_verifier,
        )
        view = change_view(session, outcome.change, _base(request), now)
    # A rejected answer is recorded (committed above) before the error is returned.
    if not outcome.verified:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_signature",
                "The signature does not match this message's signer. This message is used up: "
                "request a new owner change if it was needed.",
                {"proof_id": outcome.proof.id},
            ),
        )
    return JSONResponse(status_code=201, content=view.model_dump(mode="json"))


# --------------------------------------------------------------------------- revocation


class RevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    controller_type: Literal["ed25519", "eip155"] | None = Field(
        default=None,
        description="Which owner signs: ed25519 (the owner key, the default when there is one) "
        "or eip155 (the owner wallet).",
    )
    reason: str | None = Field(
        default=None, max_length=300, description="Optional, kept with the revocation."
    )


class RevocationChallenge(BaseModel):
    challenge_id: str
    did: str
    status: str = Field(description="open, verified, rejected or expired")
    controller: str
    message: str = Field(description="Sign exactly this text (UTF-8). It revokes for good.")
    signature_algorithm: str
    signature_encoding: str
    expires_at: datetime
    signature_url: str


def _revocation_body(
    session: Session, challenge: ControllerChallenge, did: str, base: str, now: datetime
) -> RevocationChallenge:
    return RevocationChallenge(
        challenge_id=challenge.id,
        did=did,
        status=owner_changes.message_status(session, challenge, now),
        controller=challenge.controller_value,
        message=challenge.message,
        **SIGNING[challenge.controller_type],
        expires_at=challenge.expires_at,
        signature_url=f"{base}/v1/dids/{did}/revocations/{challenge.id}/signature",
    )


@router.post(
    "/dids/{identity}/revocations",
    status_code=201,
    response_model=RevocationChallenge,
    summary="Get the message an owner signs to revoke the DID-LLM for good",
    responses={
        404: {"description": "Unknown DID-LLM."},
        409: {"description": "Already revoked, or no such owner."},
        422: {"description": "The reason is refused."},
    },
)
def create_revocation(
    body: RevocationRequest,
    request: Request,
    identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
) -> RevocationChallenge:
    state = request.app.state
    now = datetime.now(UTC)
    with transaction(state.session_factory) as session:
        challenge = owner_changes.create_revocation(
            session,
            identity,
            controller_type=body.controller_type,
            reason=body.reason,
            audience=state.settings.public_base_url,
            now=now,
        )
        record = session.get(AgentIdentity, challenge.identity_id)
        assert record is not None
        return _revocation_body(session, challenge, record.public_identifier, _base(request), now)


@router.get(
    "/dids/{identity}/revocations/{challenge_id}",
    response_model=RevocationChallenge,
    summary="A revocation message again (for tools that sign it)",
    responses={404: {"description": "Unknown DID-LLM or revocation message."}},
)
def get_revocation(
    request: Request,
    identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
    challenge_id: str = Path(pattern=CHALLENGE_ID),
) -> RevocationChallenge:
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        challenge, did = owner_changes.find_revocation(session, challenge_id)
        record = public_identity(session, identity)
        if record is None or record.id != challenge.identity_id:
            raise ApiError(404, "unknown_challenge", "Unknown revocation message for this DID-LLM.")
        return _revocation_body(session, challenge, did, _base(request), now)


class RevocationSignature(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    signature: str = Field(min_length=86, max_length=16_400)


class RevocationResult(BaseModel):
    proof_id: str
    did: str
    status: str = Field(description="The DID-LLM's status: revoked")
    revoked_at: datetime


@router.post(
    "/dids/{identity}/revocations/{challenge_id}/signature",
    status_code=201,
    response_model=RevocationResult,
    summary="Send the signed revocation message: the DID-LLM is revoked for good",
    responses={
        404: {"description": "Unknown DID-LLM or revocation message."},
        409: {"description": "Message already answered, or already revoked."},
        410: {"description": "Message expired."},
        422: {"description": "Wrong or malformed signature (a wrong one uses up the message)."},
        503: {"description": "A smart wallet's network cannot be asked right now (try again)."},
    },
)
def submit_revocation(
    body: RevocationSignature,
    request: Request,
    identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
    challenge_id: str = Path(pattern=CHALLENGE_ID),
) -> Any:
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        outcome = owner_changes.submit_revocation(
            session,
            identity,
            challenge_id=challenge_id,
            signature=body.signature,
            now=now,
            wallets=request.app.state.wallet_verifier,
        )
        result = RevocationResult(
            proof_id=outcome.proof.id,
            did=outcome.identity.public_identifier,
            status=outcome.identity.status,
            revoked_at=now,
        )
    if not outcome.verified:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_signature",
                "The signature does not match the DID-LLM's owner. This message is used up; "
                "request a new one.",
                {"proof_id": result.proof_id},
            ),
        )
    return JSONResponse(status_code=201, content=result.model_dump(mode="json"))
