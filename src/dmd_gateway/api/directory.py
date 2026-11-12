"""The DID-LLM directory API: DID-LLMs whose owners chose to be listed, with a public profile.

    GET  /v1/directory                                  the listings (search, type, pages)
    GET  /v1/directory/{identity}                       one listing
    POST /v1/directory/{identity}/challenges            the message to sign: list, change, remove
    GET  /v1/directory/challenges/{challenge_id}        that message again (for signing tools)
    POST /v1/directory/{identity}/proofs                the signed message: the change applies

{identity} is the DID-LLM or its identity ID (dmd_identity_...). Reading is open to anyone,
from any site (CORS); every change is signed by the DID-LLM's owner. See
``services/directory.py`` for the rules.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from dmd_gateway.api.registrations import SIGNING, ProofRequest
from dmd_gateway.db.models import AgentIdentity, ControllerProof, DirectoryListing
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import ApiError, error_body
from dmd_gateway.identity.did import did_document_url, did_suffix
from dmd_gateway.services import directory
from dmd_gateway.services.directory import pages_base
from dmd_gateway.services.identity_status import IDENTITY_OR_DID
from dmd_gateway.services.registrations import identity_record_url

router = APIRouter(prefix="/v1", tags=["directory"])

CHALLENGE_ID = r"^dmd_challenge_[0-9A-HJKMNP-TV-Z]{26}$"
IDENTITY_HELP = (
    "The DID-LLM (`did:web:api.debitmydata.io:agents:...`) or its identity ID (`dmd_identity_...`)."
)
# Anyone may read the directory, from any site; it changes only with signed messages.
PUBLIC = {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "public, max-age=60",
    "X-Content-Type-Options": "nosniff",
}
PAGE_PATH = "/directory"  # the directory's own page (api/directory_page.py)
LIST_PATH = "/directory/list"  # where owners list their DID-LLM


def _base(request: Request) -> str:
    return request.app.state.settings.public_base_url.rstrip("/")


def entry_url(pages: str, identity_id: str) -> str:
    return f"{pages}{PAGE_PATH}/{did_suffix(identity_id)}"


# --------------------------------------------------------------------------- reading


class DirectoryEntry(BaseModel):
    did: str
    identity_id: str
    name: str = Field(description="The DID-LLM's display name.")
    participant_type: str = Field(description="What the DID-LLM was registered for.")
    entity_type: str = Field(description="avatar, robot or sentient")
    specialty: str
    description: str
    owner_name: str | None
    region: str | None
    country: str | None
    created_on: date | None = Field(description="When the entity itself was created.")
    listed_at: datetime
    updated_at: datetime
    entry_url: str = Field(description="This listing on the directory page.")
    did_document_url: str
    identity_url: str


def entry(
    base: str, identity: AgentIdentity, listing: DirectoryListing, pages: str | None = None
) -> DirectoryEntry:
    assert listing.listed_at is not None
    return DirectoryEntry(
        did=identity.public_identifier,
        identity_id=identity.id,
        name=identity.display_name,
        participant_type=identity.participant_type,
        entity_type=listing.entity_type or "",
        specialty=listing.specialty or "",
        description=listing.description or "",
        owner_name=listing.owner_name,
        region=listing.region,
        country=listing.country,
        created_on=listing.created_on,
        listed_at=listing.listed_at,
        updated_at=listing.updated_at,
        entry_url=entry_url(pages or base, identity.id),
        did_document_url=did_document_url(base, identity.id),
        identity_url=identity_record_url(base, identity.id),
    )


class DirectoryPage(BaseModel):
    entries: list[DirectoryEntry]
    next_cursor: str | None = Field(description="Give it as `cursor` for the next page.")


@router.get(
    "/directory",
    response_model=DirectoryPage,
    summary="The DID-LLM directory: DID-LLMs whose owners chose to be listed (newest first)",
)
def listings(
    request: Request,
    q: str | None = Query(
        default=None,
        max_length=directory.MAX_SEARCH,
        description="Words to find in the name, specialty, description, owner, region or country.",
    ),
    type: Literal["avatar", "robot", "sentient"] | None = Query(  # noqa: A002
        default=None, description="Only this type."
    ),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=directory.PAGE_SIZE, ge=1, le=directory.MAX_PAGE_SIZE),
) -> JSONResponse:
    base = _base(request)
    with transaction(request.app.state.session_factory) as session:
        page = directory.search(session, text=q, entity_type=type, cursor=cursor, limit=limit)
        body = DirectoryPage(
            entries=[
                entry(base, identity, listing, pages_base(request.app.state.settings))
                for identity, listing in page.entries
            ],
            next_cursor=page.next_cursor,
        )
    return JSONResponse(body.model_dump(mode="json"), headers=PUBLIC)


@router.get(
    "/directory/{identity}",
    response_model=DirectoryEntry,
    summary="One DID-LLM's directory listing",
    responses={404: {"description": "Not in the directory (never listed, removed or hidden)."}},
)
def listing(
    request: Request, identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP)
) -> JSONResponse:
    base = _base(request)
    with transaction(request.app.state.session_factory) as session:
        found = directory.shown_entry(session, identity)
        if found is None:
            body: dict[str, Any] = error_body("not_listed", "This DID-LLM is not in the directory.")
            return JSONResponse(body, status_code=404, headers=PUBLIC)
        pages = pages_base(request.app.state.settings)
        body = entry(base, *found, pages).model_dump(mode="json")
    return JSONResponse(body, headers=PUBLIC)


# --------------------------------------------------------------------------- signed changes


class ListingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["avatar", "robot", "sentient"]
    specialty: str = Field(
        max_length=500, description="What it does, e.g. `Customer support` (3-120 characters)."
    )
    description: str = Field(
        max_length=2000,
        description="What it is and what it is for: plain text, 20-700 characters, 100 words "
        "at most, no links.",
    )
    owner_name: str | None = Field(
        default=None, max_length=500, description="Optional, public: the owner's name."
    )
    region: str | None = Field(
        default=None, max_length=500, description="Optional: state or region, e.g. `California`."
    )
    country: str | None = Field(default=None, max_length=500, description="Optional: e.g. `USA`.")
    created_on: date | None = Field(
        default=None, description="Optional: when the entity was created (YYYY-MM-DD)."
    )


class ListingChallengeRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "listed": True,
                    "profile": {
                        "entity_type": "robot",
                        "specialty": "Research assistant",
                        "description": "Finds and summarizes papers on renewable energy, "
                        "with sources for every claim.",
                        "country": "USA",
                    },
                },
                {"listed": False},
            ]
        },
    )

    listed: bool = Field(
        default=True,
        description="true: list the DID-LLM with this profile (or change it); false: remove it "
        "from the directory.",
    )
    profile: ListingProfile | None = Field(
        default=None, description="The public profile (needed when `listed` is true)."
    )
    controller_type: Literal["ed25519", "eip155"] | None = Field(
        default=None,
        description="Who signs: ed25519 (the owner key) or eip155 (the owner wallet). Left out: "
        "the key if the DID-LLM has one, else the wallet.",
    )


class ListingChallenge(BaseModel):
    challenge_id: str
    did: str
    listed: bool = Field(description="What signing does: list (true) or remove (false).")
    status: str = Field(description="open, verified, rejected or expired")
    controller: str
    message: str = Field(description="Sign exactly this text (UTF-8).")
    signature_algorithm: str
    signature_encoding: str
    expires_at: datetime
    proof_url: str


def _challenge_body(base: str, challenge: Any, did: str, status: str) -> ListingChallenge:
    proposal = challenge.proposal or {}
    return ListingChallenge(
        challenge_id=challenge.id,
        did=did,
        listed=bool(proposal.get("listed")),
        status=status,
        controller=challenge.controller_value,
        message=challenge.message,
        **SIGNING[challenge.controller_type],
        expires_at=challenge.expires_at,
        proof_url=f"{base}/v1/directory/{did}/proofs",
    )


@router.post(
    "/directory/{identity}/challenges",
    status_code=201,
    response_model=ListingChallenge,
    summary="Get the message the owner signs to list a DID-LLM, change its listing or remove it",
    responses={
        404: {"description": "Unknown DID-LLM."},
        409: {"description": "Not active (listing), not listed (removal), or no such signer."},
        422: {"description": "The profile is refused (`error.field` says which field)."},
        429: {"description": "Too many open listing messages for this DID-LLM."},
    },
)
def create_challenge(
    body: ListingChallengeRequest,
    request: Request,
    identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
) -> ListingChallenge:
    state = request.app.state
    if body.listed and body.profile is None:
        raise ApiError(422, "invalid_profile", "Give the profile to list.", details={"field": ""})
    with transaction(state.session_factory) as session:
        challenge = directory.create_challenge(
            session,
            identity,
            listed=body.listed,
            raw_profile=body.profile.model_dump() if body.listed and body.profile else None,
            controller_type=body.controller_type,
            audience=state.settings.public_base_url,
            terms_version=state.policy_pages["terms"].version.isoformat(),
            now=datetime.now(UTC),
            brand_owners=state.quote_context.brand_owners,
        )
        session.flush()
        record = session.get(AgentIdentity, challenge.identity_id)
        assert record is not None
        return _challenge_body(_base(request), challenge, record.public_identifier, "open")


@router.get(
    "/directory/challenges/{challenge_id}",
    response_model=ListingChallenge,
    summary="A directory listing message again, by its ID (for tools that sign it)",
    responses={404: {"description": "Unknown listing message."}},
)
def get_challenge(
    request: Request, challenge_id: str = Path(pattern=CHALLENGE_ID)
) -> ListingChallenge:
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        challenge, did = directory.find_challenge(session, challenge_id)
        status = "open"
        if challenge.used_at is not None:
            proof = session.scalar(
                select(ControllerProof).where(ControllerProof.challenge_id == challenge.id)
            )
            status = proof.status if proof is not None else "used"
        elif challenge.expires_at <= now:
            status = "expired"
        return _challenge_body(_base(request), challenge, did, status)


class ListingProofResponse(BaseModel):
    proof_id: str
    did: str
    status: str
    listed: bool
    shown: bool = Field(
        description="Whether the directory shows it now (false when removed, or when an "
        "operator hid it)."
    )
    entry: DirectoryEntry | None


@router.post(
    "/directory/{identity}/proofs",
    status_code=201,
    response_model=ListingProofResponse,
    summary="Send the signed listing message: the listing change applies as it was signed",
    responses={
        404: {"description": "Unknown DID-LLM or listing message."},
        409: {"description": "Message already answered, or the DID-LLM is not active."},
        410: {"description": "Message expired."},
        422: {"description": "Wrong or malformed signature (a wrong one uses up the message)."},
        503: {"description": "A smart wallet's network cannot be asked right now (try again)."},
    },
)
def submit_proof(
    body: ProofRequest,
    request: Request,
    identity: str = Path(pattern=IDENTITY_OR_DID, description=IDENTITY_HELP),
) -> Any:
    base = _base(request)
    with transaction(request.app.state.session_factory) as session:
        outcome = directory.submit_proof(
            session,
            identity,
            challenge_id=body.challenge_id,
            signature=body.signature,
            now=datetime.now(UTC),
            wallets=request.app.state.wallet_verifier,
        )
        shown = directory.is_shown(outcome.identity, outcome.listing)
        response = ListingProofResponse(
            proof_id=outcome.proof.id,
            did=outcome.identity.public_identifier,
            status=outcome.proof.status,
            listed=bool(outcome.listing and outcome.listing.listed),
            shown=shown,
            entry=entry(
                base, outcome.identity, outcome.listing, pages_base(request.app.state.settings)
            )
            if shown and outcome.listing
            else None,
        )
    # The rejected answer is recorded (committed above) before the error is returned.
    if not outcome.verified:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_signature",
                "The signature does not match the DID-LLM's owner. This message is used up; "
                "request a new one.",
                {"proof_id": response.proof_id},
            ),
        )
    return JSONResponse(status_code=201, content=response.model_dump(mode="json"))
