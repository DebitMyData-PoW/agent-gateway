"""The DID-LLM directory: a public list of DID-LLMs whose owners chose to be listed.

Nobody is listed unless the owner asks for it. A listing, a change to it and its removal are
each signed by the DID-LLM's owner, with the controller key or wallet that proved control when
the DID-LLM was issued, exactly like that proof:

1. the owner sends the profile and gets a one-time message (a controller challenge for the
   purpose ``directory_listing``) that spells it out, line by line;
2. the owner signs that exact message (Ed25519, or the wallet's personal_sign) and sends the
   signature; once it matches, the change is applied, as it was signed.

The profile has the Agentic Hub's fields, without pictures: the type (avatar, robot or
sentient), the specialty, a short description, and optionally the owner's name, a region, a
country and the date the entity was created. Its name is the DID-LLM's own display name, which
was checked when the DID-LLM was quoted. Everything is plain text, on one line, with no web
addresses, e-mail addresses or hidden characters.

A listing is shown while it is listed, its DID-LLM is active (not suspended or revoked), and no
operator has hidden it (``dmd-gateway-admin directory hide``, for a listing that breaks the
rules). Every change is in the audit trail, and the monitor reports each new or changed
listing, so that someone checks it.
"""

from __future__ import annotations

import base64
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session

from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import (
    DIRECTORY_ENTITY_TYPES,
    AgentIdentity,
    ControllerChallenge,
    ControllerProof,
    DirectoryListing,
)
from dmd_gateway.domain.ids import CHALLENGE, PROOF, new_id
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import xml_datetime
from dmd_gateway.identity.names import NameRefused, check_display_name, is_brand_owner
from dmd_gateway.identity.wallets import WalletVerifier
from dmd_gateway.services.controller_proofs import ED25519, EIP155, check_signature
from dmd_gateway.services.identity_status import (
    OPERATOR,
    REASON_LENGTH,
    StatusChangeError,
    find_identity,
    public_identity,
)
from dmd_gateway.settings import Settings

PURPOSE = "directory_listing"
MESSAGE_TITLE = "DebitMyData directory listing"
# Long enough to sign with a tool in another window.
CHALLENGE_TTL = timedelta(minutes=15)
MAX_OPEN_CHALLENGES = 5
ENTITY_LABELS = {"avatar": "Avatar", "robot": "Robot", "sentient": "Sentient"}
assert tuple(ENTITY_LABELS) == DIRECTORY_ENTITY_TYPES
# (shortest, longest) in characters; the description also has a word limit.
LENGTHS = {
    "specialty": (3, 120),
    "description": (20, 700),
    "owner_name": (2, 80),
    "region": (2, 60),
    "country": (2, 60),
}
DESCRIPTION_WORDS = 100
LABELS = {
    "entity_type": "The type",
    "specialty": "The specialty",
    "description": "The description",
    "owner_name": "The owner's name",
    "region": "The region",
    "country": "The country",
    "created_on": "The creation date",
}
OLDEST_CREATION = date(1950, 1, 1)
PAGE_SIZE = 24
MAX_PAGE_SIZE = 50
MAX_SEARCH = 80
MAX_SEARCH_WORDS = 6

LISTED = "directory.listed"
UPDATED = "directory.updated"
UNLISTED = "directory.unlisted"
HIDDEN = "directory.hidden"
SHOWN_AGAIN = "directory.shown_again"
# The monitor reports these, so that someone looks at each new or changed listing.
REPORTED = (LISTED, UPDATED, HIDDEN, SHOWN_AGAIN)

_LINK = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}")
_HIDDEN_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"}
_JOINERS = {"‌", "‍"}  # needed in some scripts and in emoji


class ProfileError(ValueError):
    """The profile is refused; ``field`` names the field, the message says why."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class Profile:
    entity_type: str
    specialty: str
    description: str
    owner_name: str | None = None
    region: str | None = None
    country: str | None = None
    created_on: date | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_on"] = self.created_on.isoformat() if self.created_on else None
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Profile:
        created = data.get("created_on")
        return cls(
            entity_type=str(data["entity_type"]),
            specialty=str(data["specialty"]),
            description=str(data["description"]),
            owner_name=data.get("owner_name"),
            region=data.get("region"),
            country=data.get("country"),
            created_on=date.fromisoformat(created) if created else None,
        )


PROFILE_FIELDS = tuple(field.name for field in fields(Profile))


# --------------------------------------------------------------------------- the profile


def clean_profile(raw: Mapping[str, Any], *, today: date, brand_owner: bool = False) -> Profile:
    """The profile as it will be published, or ProfileError: spaces collapsed to one line,
    empty optional fields left out."""
    entity_type = str(raw.get("entity_type") or "").strip().lower()
    if entity_type not in ENTITY_LABELS:
        raise ProfileError("entity_type", "Choose the type: avatar, robot or sentient.")
    texts: dict[str, str | None] = {}
    for field in LENGTHS:
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise ProfileError(field, f"{LABELS[field]} must be text.")
        texts[field] = _clean_text(
            field, value or "", required=field in ("specialty", "description")
        )
    words = len((texts["description"] or "").split())
    if words > DESCRIPTION_WORDS:
        raise ProfileError(
            "description",
            f"The description has {words} words: keep it to {DESCRIPTION_WORDS} words at most.",
        )
    if texts["owner_name"]:
        try:
            check_display_name(texts["owner_name"], brand_owner=brand_owner)
        except NameRefused as exc:
            raise ProfileError(
                "owner_name", "The owner's name: " + str(exc).replace("The name", "it")
            ) from exc
    created_on = _creation_date(raw.get("created_on"), today)
    return Profile(
        entity_type=entity_type,
        specialty=texts["specialty"] or "",
        description=texts["description"] or "",
        owner_name=texts["owner_name"],
        region=texts["region"],
        country=texts["country"],
        created_on=created_on,
    )


def _clean_text(field: str, value: str, *, required: bool) -> str | None:
    label = LABELS[field]
    for ch in value:
        if ch in _JOINERS:
            continue
        if unicodedata.category(ch) in _HIDDEN_CATEGORIES and ch not in "\n\r\t":
            raise ProfileError(
                field,
                f"{label} contains a hidden character (U+{ord(ch):04X}), such as a zero-width "
                "or text-direction mark. Type it again without it.",
            )
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not text:
        if required:
            raise ProfileError(field, f"{label} is required.")
        return None
    low, high = LENGTHS[field]
    if not low <= len(text) <= high:
        raise ProfileError(field, f"{label} must be {low} to {high} characters long.")
    if _LINK.search(text):
        raise ProfileError(field, f"{label} cannot contain web addresses (links).")
    if _EMAIL.search(text):
        raise ProfileError(field, f"{label} cannot contain e-mail addresses.")
    return text


def _creation_date(value: Any, today: date) -> date | None:
    if value in (None, ""):
        return None
    try:
        created = value if isinstance(value, date) else date.fromisoformat(str(value))
    except ValueError as exc:
        raise ProfileError("created_on", "The creation date is a date: YYYY-MM-DD.") from exc
    if not OLDEST_CREATION <= created <= today:
        raise ProfileError(
            "created_on", "The creation date must be a real date, not in the future."
        )
    return created


# --------------------------------------------------------------------------- the message


def listing_message(
    *,
    audience: str,
    did: str,
    controller: str,
    name: str,
    profile: Profile | None,
    terms_version: str,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    """The exact text the owner signs (UTF-8, lines joined by \\n, no trailing newline). Every
    value is on one line, so no value can add a line of its own. ``profile`` None: removal."""
    lines = [
        MESSAGE_TITLE,
        f"Audience: {audience}",
        f"Purpose: {PURPOSE}",
        f"DID-LLM: {did}",
        f"Controller: {controller}",
    ]
    if profile is None:
        lines.append("Listed: no (remove it from the directory)")
    else:
        lines += [
            "Listed: yes (public)",
            f"Name: {name}",
            f"Type: {ENTITY_LABELS[profile.entity_type]}",
            f"Specialty: {profile.specialty}",
            f"Description: {profile.description}",
            f"Owner: {profile.owner_name or '-'}",
            f"Region: {profile.region or '-'}",
            f"Country: {profile.country or '-'}",
            f"Created: {profile.created_on.isoformat() if profile.created_on else '-'}",
            f"Terms: {terms_version}",
        ]
    lines += [
        f"Nonce: {nonce}",
        f"Issued: {xml_datetime(issued_at)}",
        f"Expires: {xml_datetime(expires_at)}",
    ]
    return "\n".join(lines)


def controllers(identity: AgentIdentity) -> dict[str, str]:
    """Controller type -> value, as in the registration's challenges."""
    found: dict[str, str] = {}
    if identity.controller_public_key:
        found[ED25519] = identity.controller_public_key
    if identity.controller_wallet_address and identity.controller_network:
        found[EIP155] = f"{identity.controller_network}:{identity.controller_wallet_address}"
    return found


def _identity(session: Session, text: str, *, lock: bool = False) -> AgentIdentity:
    identity = public_identity(session, text, lock=lock)
    if identity is None:
        raise ApiError(404, "not_found", "Unknown DID-LLM.")
    return identity


def _choose(identity: AgentIdentity, controller_type: str | None) -> tuple[str, str]:
    found = controllers(identity)
    if controller_type is None:
        controller_type = ED25519 if ED25519 in found else next(iter(found))
    if controller_type not in found:
        raise ApiError(
            409,
            "no_such_controller",
            f"This DID-LLM has no {controller_type} controller; it has: " + ", ".join(found),
        )
    return controller_type, found[controller_type]


# --------------------------------------------------------------------------- signed changes


def create_challenge(
    session: Session,
    identity_text: str,
    *,
    listed: bool,
    raw_profile: Mapping[str, Any] | None,
    controller_type: str | None,
    audience: str,
    terms_version: str,
    now: datetime,
    brand_owners: frozenset[str] = frozenset(),
) -> ControllerChallenge:
    """A one-time message for the owner to sign: list (or change) the DID-LLM with
    ``raw_profile`` (``listed``), or remove its listing."""
    now = now.replace(microsecond=0)
    identity = _identity(session, identity_text, lock=True)
    profile = None
    if listed:
        if identity.status != "active":
            raise ApiError(
                409,
                "identity_not_active",
                f"This DID-LLM is {identity.status}: only an active DID-LLM can be listed.",
            )
        brand = is_brand_owner(
            brand_owners, identity.controller_public_key, identity.controller_wallet_address
        )
        try:
            profile = clean_profile(raw_profile or {}, today=now.date(), brand_owner=brand)
        except ProfileError as exc:
            raise ApiError(422, "invalid_profile", str(exc), details={"field": exc.field}) from exc
    else:
        listing = session.get(DirectoryListing, identity.id)
        if listing is None or not listing.listed:
            raise ApiError(409, "not_listed", "This DID-LLM is not in the directory.")
    kind, controller = _choose(identity, controller_type)
    open_challenges = session.scalar(
        select(func.count())
        .select_from(ControllerChallenge)
        .where(
            ControllerChallenge.identity_id == identity.id,
            ControllerChallenge.purpose == PURPOSE,
            ControllerChallenge.used_at.is_(None),
            ControllerChallenge.expires_at > now,
        )
    )
    if open_challenges >= MAX_OPEN_CHALLENGES:
        raise ApiError(
            429,
            "too_many_challenges",
            "Too many open listing messages for this DID-LLM; sign one or wait for them to expire.",
            headers={"Retry-After": str(int(CHALLENGE_TTL.total_seconds()))},
        )
    nonce = secrets.token_urlsafe(32)
    expires_at = now + CHALLENGE_TTL
    proposal = {
        "listed": profile is not None,
        "profile": profile.to_json() if profile else None,
        "terms_version": terms_version,
    }
    challenge = ControllerChallenge(
        id=new_id(CHALLENGE),
        identity_id=identity.id,
        purpose=PURPOSE,
        controller_type=kind,
        controller_value=controller,
        nonce=nonce,
        message=listing_message(
            audience=audience,
            did=identity.public_identifier,
            controller=controller,
            name=identity.display_name,
            profile=profile,
            terms_version=terms_version,
            nonce=nonce,
            issued_at=now,
            expires_at=expires_at,
        ),
        proposal=proposal,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(challenge)
    record_audit_event(
        session,
        action="directory.challenge_created",
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        data={
            "challenge_id": challenge.id,
            "controller_type": kind,
            "listed": proposal["listed"],
            "expires_at": xml_datetime(expires_at),
        },
    )
    return challenge


def find_challenge(session: Session, challenge_id: str) -> tuple[ControllerChallenge, str]:
    """A listing message by its ID, with its DID-LLM (for tools that only have the ID)."""
    challenge = session.get(ControllerChallenge, challenge_id)
    if challenge is None or challenge.purpose != PURPOSE or challenge.identity_id is None:
        raise ApiError(404, "unknown_challenge", "Unknown directory listing message.")
    identity = session.get(AgentIdentity, challenge.identity_id)
    assert identity is not None
    return challenge, identity.public_identifier


@dataclass(frozen=True)
class ListingOutcome:
    proof: ControllerProof
    verified: bool
    identity: AgentIdentity
    listing: DirectoryListing | None


def submit_proof(
    session: Session,
    identity_text: str,
    *,
    challenge_id: str,
    signature: str,
    now: datetime,
    wallets: WalletVerifier | None = None,
) -> ListingOutcome:
    """Check the owner's signature and, if it matches, apply the change as it was signed.
    The caller commits even when the signature is rejected (the answer is recorded)."""
    # Lock order (identity, then challenge) matches create_challenge.
    identity = _identity(session, identity_text, lock=True)
    challenge = session.get(ControllerChallenge, challenge_id, with_for_update=True)
    if (
        challenge is None
        or challenge.purpose != PURPOSE
        or challenge.identity_id != identity.id
        or challenge.proposal is None
    ):
        raise ApiError(404, "unknown_challenge", "Unknown listing message for this DID-LLM.")
    if challenge.used_at is not None:
        raise ApiError(
            409, "challenge_already_used", "This message was already answered; request a new one."
        )
    if challenge.expires_at <= now:
        raise ApiError(410, "challenge_expired", "This message has expired; request a new one.")
    listed = bool(challenge.proposal.get("listed"))
    if listed and identity.status != "active":
        raise ApiError(
            409,
            "identity_not_active",
            f"This DID-LLM is {identity.status}: only an active DID-LLM can be listed.",
        )

    still_controller = controllers(identity).get(challenge.controller_type) == (
        challenge.controller_value
    )
    check = check_signature(
        challenge.controller_type,
        challenge.controller_value,
        challenge.message,
        signature,
        still_controller=still_controller,
        wallets=wallets,
    )
    reason = None
    if not still_controller:
        reason = "the DID-LLM's controller changed after this message was issued"
    elif not check.verified:
        reason = f"the signature does not match {check.what}"
    challenge.used_at = now
    proof = ControllerProof(
        id=new_id(PROOF),
        challenge_id=challenge.id,
        method=check.method,
        signature=check.signature,
        status="verified" if check.verified else "rejected",
        verified_at=now if check.verified else None,
        failure_reason=reason,
        created_at=now,
    )
    session.add(proof)
    session.flush()  # the listing refers to the proof
    record_audit_event(
        session,
        action="controller_proof.verified" if check.verified else "controller_proof.rejected",
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        data={
            "purpose": PURPOSE,
            "challenge_id": challenge.id,
            "proof_id": proof.id,
            "method": check.method,
            "controller": challenge.controller_value,
            **({"reason": reason} if reason else {}),
        },
    )
    if not check.verified:
        return ListingOutcome(proof, False, identity, session.get(DirectoryListing, identity.id))
    listing = _apply(session, identity, challenge, proof, now)
    return ListingOutcome(proof, True, identity, listing)


def _apply(
    session: Session,
    identity: AgentIdentity,
    challenge: ControllerChallenge,
    proof: ControllerProof,
    now: datetime,
) -> DirectoryListing:
    proposal = challenge.proposal or {}
    listing = session.get(DirectoryListing, identity.id, with_for_update=True)
    was_listed = bool(listing and listing.listed)
    if listing is None:
        listing = DirectoryListing(identity_id=identity.id, created_at=now)
        session.add(listing)
    listing.terms_version = str(proposal.get("terms_version"))
    listing.challenge_id = challenge.id
    listing.proof_id = proof.id
    listing.updated_at = now
    if proposal.get("listed"):
        profile = Profile.from_json(proposal["profile"])
        for field, value in asdict(profile).items():
            setattr(listing, field, value)
        listing.listed = True
        if not was_listed:
            listing.listed_at = now
        action = UPDATED if was_listed else LISTED
    else:
        # Removed: the profile goes too (the signed messages stay as the record).
        for field in PROFILE_FIELDS:
            setattr(listing, field, None)
        listing.listed = False
        listing.listed_at = None
        action = UNLISTED
    record_audit_event(
        session,
        action=action,
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        data={
            "did": identity.public_identifier,
            "challenge_id": challenge.id,
            "proof_id": proof.id,
            "terms_version": listing.terms_version,
        },
    )
    session.flush()
    return listing


# --------------------------------------------------------------------------- reading


def pages_base(settings: Settings) -> str:
    """Where people see the directory: the app's address (id.debitmydata.io) when there is
    one, else this server. The directory's pages on the API's address lead there."""
    if settings.app_host:
        return f"https://{settings.app_host}"
    return settings.public_base_url.rstrip("/")


def _shown():
    return and_(
        DirectoryListing.listed.is_(True),
        DirectoryListing.hidden_at.is_(None),
        AgentIdentity.status == "active",
    )


def is_shown(identity: AgentIdentity, listing: DirectoryListing | None) -> bool:
    return bool(
        listing is not None
        and listing.listed
        and listing.hidden_at is None
        and identity.status == "active"
    )


def shown_entry(session: Session, text: str) -> tuple[AgentIdentity, DirectoryListing] | None:
    identity = public_identity(session, text)
    if identity is None:
        return None
    listing = session.get(DirectoryListing, identity.id)
    if not is_shown(identity, listing):
        return None
    assert listing is not None
    return identity, listing


@dataclass(frozen=True)
class Page:
    entries: list[tuple[AgentIdentity, DirectoryListing]]
    next_cursor: str | None


def encode_cursor(listing: DirectoryListing) -> str:
    assert listing.listed_at is not None
    raw = f"{listing.listed_at.isoformat()}|{listing.identity_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        moment, identity_id = raw.split("|", 1)
        when = datetime.fromisoformat(moment)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(422, "invalid_cursor", "This cursor is not one the directory gave.") from exc
    if when.tzinfo is None or not identity_id.startswith("dmd_identity_"):
        raise ApiError(422, "invalid_cursor", "This cursor is not one the directory gave.")
    return when, identity_id


def search(
    session: Session,
    *,
    text: str | None = None,
    entity_type: str | None = None,
    cursor: str | None = None,
    limit: int = PAGE_SIZE,
) -> Page:
    """The shown listings, newest first, matching every word of ``text`` (in the name, the
    specialty, the description, the owner's name, the region or the country)."""
    query = (
        select(AgentIdentity, DirectoryListing)
        .join(DirectoryListing, DirectoryListing.identity_id == AgentIdentity.id)
        .where(_shown())
    )
    if entity_type:
        query = query.where(DirectoryListing.entity_type == entity_type)
    for word in (text or "").split()[:MAX_SEARCH_WORDS]:
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", word) + "%"
        query = query.where(
            or_(
                AgentIdentity.display_name.ilike(pattern, escape="\\"),
                DirectoryListing.specialty.ilike(pattern, escape="\\"),
                DirectoryListing.description.ilike(pattern, escape="\\"),
                DirectoryListing.owner_name.ilike(pattern, escape="\\"),
                DirectoryListing.region.ilike(pattern, escape="\\"),
                DirectoryListing.country.ilike(pattern, escape="\\"),
            )
        )
    if cursor:
        when, identity_id = decode_cursor(cursor)
        query = query.where(
            tuple_(DirectoryListing.listed_at, DirectoryListing.identity_id)
            < tuple_(when, identity_id)
        )
    rows = session.execute(
        query.order_by(
            DirectoryListing.listed_at.desc(), DirectoryListing.identity_id.desc()
        ).limit(limit + 1)
    ).all()
    entries = [(identity, listing) for identity, listing in rows]
    more = len(entries) > limit
    entries = entries[:limit]
    return Page(entries, encode_cursor(entries[-1][1]) if more and entries else None)


def count_shown(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(DirectoryListing)
            .join(AgentIdentity, AgentIdentity.id == DirectoryListing.identity_id)
            .where(_shown())
        )
        or 0
    )


# --------------------------------------------------------------------------- operators


@dataclass(frozen=True)
class OperatorChange:
    identity: AgentIdentity
    listing: DirectoryListing
    audit_event_id: int


def set_hidden(
    session: Session,
    text: str,
    *,
    hide: bool,
    reason: str,
    operator: str,
    now: datetime,
) -> OperatorChange:
    """Hide a listing that breaks the rules, or show it again (``dmd-gateway-admin``)."""
    reason = " ".join(reason.split())
    low, high = REASON_LENGTH
    if not low <= len(reason) <= high:
        raise StatusChangeError(f"Give a reason of {low} to {high} characters.")
    if not OPERATOR.fullmatch(operator.strip()):
        raise StatusChangeError(
            "Give your name with --by (letters, digits, spaces, '.', '_', '@' or '-')."
        )
    operator = operator.strip()
    identity = find_identity(session, text, lock=True)
    listing = session.get(DirectoryListing, identity.id, with_for_update=True)
    if listing is None:
        raise StatusChangeError("This DID-LLM was never listed in the directory.")
    if hide and listing.hidden_at is not None:
        raise StatusChangeError("This listing is already hidden: nothing was changed.")
    if not hide and listing.hidden_at is None:
        raise StatusChangeError("This listing is not hidden: nothing was changed.")
    if hide:
        listing.hidden_at, listing.hidden_reason, listing.hidden_by = now, reason, operator
    else:
        listing.hidden_at = listing.hidden_reason = listing.hidden_by = None
    listing.updated_at = now
    event = record_audit_event(
        session,
        action=HIDDEN if hide else SHOWN_AGAIN,
        subject_type="identity",
        subject_id=identity.id,
        actor_type="operator",
        actor_id=operator,
        data={"did": identity.public_identifier, "reason": reason},
    )
    session.flush()
    return OperatorChange(identity, listing, int(event.id))
