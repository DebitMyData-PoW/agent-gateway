"""Owner changes and owner revocation of an issued DID-LLM (ID-12).

An owner change replaces the DID-LLM's owner key and/or wallet (its controllers). The request
names the owners it will have afterwards, and the change applies once, within 15 minutes:

* one of its current owners **approves** it: a current owner key or wallet signs a message
  that names the DID-LLM, the owners now and the new ones;
* each new owner that is not already one **accepts** it: the new key or wallet signs the same
  kind of message, which proves it exists and is in the new owner's hands (nobody can hand a
  DID-LLM to a key nobody holds, or to a mistyped address).

Signed exactly like the registration's controller proof (Ed25519, or the wallet's
personal_sign, smart wallets included), each message answered once. Either current owner may
approve, as either may sign in as the DID-LLM. Once applied:

* the DID-LLM's document and record name the new owners, and only they can sign for it;
* every sign-in made before the change ends: apps' refresh tokens are revoked, and access
  tokens and app sign-ins (grants) issued before ``identities.controller_changed_at`` are
  refused, by the API and by the token signer;
* its credential stays (the registration receipt, naming who registered it), its directory
  listing stays, and other open changes are cancelled;
* the audit trail keeps the owners before and after.

An owner may also **revoke** the DID-LLM for good, with one signed message that says so; it
then stops working, with its credential and entitlements (identity_status.set_status), like
an operator's revocation. Nothing on the public API brings it back.

A lost key is not covered here: with no current owner able to sign, nothing can be changed
(recovery is a policy of its own, ID-14).
"""

from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import (
    AgentIdentity,
    ControllerChallenge,
    ControllerProof,
    OAuthRefreshToken,
    OwnerChange,
)
from dmd_gateway.domain.ids import CHALLENGE, OWNER_CHANGE, PROOF, new_id
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import xml_datetime
from dmd_gateway.identity.names import NameRefused, check_display_name, is_brand_owner
from dmd_gateway.identity.wallets import Wallet, WalletVerifier
from dmd_gateway.services.controller_proofs import ED25519, EIP155, check_signature
from dmd_gateway.services.directory import controllers
from dmd_gateway.services.identity_status import (
    REASON_LENGTH,
    StatusChangeError,
    public_identity,
    set_status,
)

CHANGE_PURPOSE = "rotation"
REVOKE_PURPOSE = "revocation"
CHANGE_TITLE = "DebitMyData owner change"
REVOKE_TITLE = "DebitMyData revocation"
TTL = timedelta(minutes=15)  # long enough to sign with tools in other windows
MAX_OPEN = 3  # open changes, or open revocation messages, per DID-LLM
APPROVE = "approve"
ACCEPT = "accept"
ROLES = {
    APPROVE: "approve (a current owner hands the DID-LLM over)",
    ACCEPT: "accept (a new owner takes the DID-LLM)",
}
OWNER_NAMES = {ED25519: "key", EIP155: "wallet"}
DEFAULT_REASON = "revoked by its owner"
_HIDDEN = {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"}

REQUESTED = "owner_change.requested"
OWNER_CHANGED = "identity.owner_changed"
REVOCATION_REQUESTED = "revocation.requested"


# --------------------------------------------------------------------------- owners


@dataclass(frozen=True)
class Owners:
    """A DID-LLM's owners: its Ed25519 key and/or its wallet."""

    public_key: str | None
    wallet: Wallet | None

    def controllers(self) -> dict[str, str]:
        found: dict[str, str] = {}
        if self.public_key:
            found[ED25519] = self.public_key
        if self.wallet:
            found[EIP155] = self.wallet.account_id
        return found


def describe(found: dict[str, str]) -> str:
    """``key ed25519:z6Mk..., wallet eip155:8453:0x...`` (one line)."""
    return ", ".join(f"{OWNER_NAMES[kind]} {value}" for kind, value in found.items()) or "-"


def previous_owners(change: OwnerChange) -> dict[str, str]:
    found: dict[str, str] = {}
    if change.previous_public_key:
        found[ED25519] = change.previous_public_key
    if change.previous_wallet_address and change.previous_network:
        found[EIP155] = f"{change.previous_network}:{change.previous_wallet_address}"
    return found


def new_owners(change: OwnerChange) -> dict[str, str]:
    found: dict[str, str] = {}
    if change.new_public_key:
        found[ED25519] = change.new_public_key
    if change.new_wallet_address and change.new_network:
        found[EIP155] = f"{change.new_network}:{change.new_wallet_address}"
    return found


def role(change: OwnerChange, controller_type: str, controller_value: str) -> str:
    """A current owner approves; a new owner (not already one) accepts."""
    return APPROVE if previous_owners(change).get(controller_type) == controller_value else ACCEPT


def _active(session: Session, identity_text: str) -> AgentIdentity:
    identity = public_identity(session, identity_text, lock=True)
    if identity is None:
        raise ApiError(404, "not_found", "Unknown DID-LLM.")
    if identity.status != "active":
        raise ApiError(
            409,
            "identity_not_active",
            f"This DID-LLM is {identity.status}: only an active DID-LLM changes owner.",
        )
    return identity


def _check_reserved_name(
    identity: AgentIdentity, new: Owners, brand_owners: frozenset[str]
) -> None:
    """A DID-LLM with one of DebitMyData's own names stays with DebitMyData's keys."""
    try:
        check_display_name(identity.display_name, brand_owner=False)
        return
    except NameRefused:
        pass
    try:
        check_display_name(identity.display_name, brand_owner=True)
    except NameRefused:
        return  # refused for another reason (rules changed since): not about the brand
    address = new.wallet.address if new.wallet else None
    if not is_brand_owner(brand_owners, new.public_key, address):
        raise ApiError(
            409,
            "reserved_name",
            "This DID-LLM's name is reserved for DebitMyData: it can only be owned by "
            "DebitMyData's own keys and wallets.",
        )


# --------------------------------------------------------------------------- the messages


def change_message(
    *,
    audience: str,
    did: str,
    change_id: str,
    signer: str,
    signer_role: str,
    owners_now: dict[str, str],
    new_owners: dict[str, str],
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    """The exact text an owner signs (UTF-8, lines joined by \\n, no trailing newline)."""
    return "\n".join(
        [
            CHANGE_TITLE,
            f"Audience: {audience}",
            f"Purpose: {CHANGE_PURPOSE}",
            f"DID-LLM: {did}",
            f"Change: {change_id}",
            f"Signer: {signer}",
            f"Role: {ROLES[signer_role]}",
            f"Owners now: {describe(owners_now)}",
            f"New owners: {describe(new_owners)}",
            "Effect: only the new owners control the DID-LLM; sign-ins made before the change end",
            f"Nonce: {nonce}",
            f"Issued: {xml_datetime(issued_at)}",
            f"Expires: {xml_datetime(expires_at)}",
        ]
    )


def revocation_message(
    *,
    audience: str,
    did: str,
    controller: str,
    reason: str,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    return "\n".join(
        [
            REVOKE_TITLE,
            f"Audience: {audience}",
            f"Purpose: {REVOKE_PURPOSE}",
            f"DID-LLM: {did}",
            f"Controller: {controller}",
            "Effect: revoked for good; it cannot be undone. The DID-LLM, its credential and "
            "its entitlements stop working forever",
            f"Reason: {reason}",
            f"Nonce: {nonce}",
            f"Issued: {xml_datetime(issued_at)}",
            f"Expires: {xml_datetime(expires_at)}",
        ]
    )


def _reason_in(message: str) -> str:
    for line in message.split("\n"):
        if line.startswith("Reason: "):
            return line.removeprefix("Reason: ")
    return DEFAULT_REASON


def clean_reason(reason: str | None) -> str:
    """The reason, on one line, as it will be signed; no hidden characters (such as text
    direction marks), so the message reads as it is."""
    if any(unicodedata.category(ch) in _HIDDEN for ch in reason or "" if ch not in "\n\r\t"):
        raise ApiError(422, "invalid_reason", "The reason contains a hidden character.")
    text = " ".join((reason or "").split())
    if not text:
        return DEFAULT_REASON
    low, high = REASON_LENGTH
    if not low <= len(text) <= high:
        raise ApiError(422, "invalid_reason", f"Give a reason of {low} to {high} characters.")
    return text


def _make_room(session: Session, identity_id: str, purpose: str, now: datetime) -> None:
    """At most MAX_OPEN open changes (or revocation messages) per DID-LLM: the oldest ones
    close to make room. Never a refusal: nobody can keep the owner from asking, by asking
    first."""
    if purpose == CHANGE_PURPOSE:
        open_changes = session.scalars(
            select(OwnerChange)
            .where(
                OwnerChange.identity_id == identity_id,
                OwnerChange.status == "open",
                OwnerChange.expires_at > now,
            )
            .order_by(OwnerChange.created_at.desc(), OwnerChange.id.desc())
            .with_for_update()
        ).all()
        for change in open_changes[MAX_OPEN - 1 :]:
            change.status = "cancelled"
        return
    open_messages = session.scalars(
        select(ControllerChallenge)
        .where(
            ControllerChallenge.identity_id == identity_id,
            ControllerChallenge.purpose == purpose,
            ControllerChallenge.used_at.is_(None),
            ControllerChallenge.expires_at > now,
        )
        .order_by(ControllerChallenge.created_at.desc(), ControllerChallenge.id.desc())
        .with_for_update()
    ).all()
    for message in open_messages[MAX_OPEN - 1 :]:
        message.expires_at = now  # closed: it can no longer be answered


# --------------------------------------------------------------------------- owner changes


def create_change(
    session: Session,
    identity_text: str,
    *,
    new: Owners,
    audience: str,
    now: datetime,
    brand_owners: frozenset[str] = frozenset(),
) -> OwnerChange:
    """An open owner change and its messages: one for each current owner (any one approves)
    and one for each new owner that is not already one (each accepts)."""
    now = now.replace(microsecond=0)
    identity = _active(session, identity_text)
    current = controllers(identity)
    wanted = new.controllers()
    if not wanted:
        raise ApiError(422, "invalid_request", "Name the new owner key and/or wallet.")
    if wanted == current:
        raise ApiError(409, "no_change", "These are already the DID-LLM's owners.")
    _check_reserved_name(identity, new, brand_owners)
    _make_room(session, identity.id, CHANGE_PURPOSE, now)
    expires_at = now + TTL
    change = OwnerChange(
        id=new_id(OWNER_CHANGE),
        identity_id=identity.id,
        previous_public_key=identity.controller_public_key,
        previous_wallet_address=identity.controller_wallet_address,
        previous_network=identity.controller_network,
        new_public_key=new.public_key,
        new_wallet_address=new.wallet.address if new.wallet else None,
        new_network=new.wallet.network if new.wallet else None,
        status="open",
        expires_at=expires_at,
        created_at=now,
    )
    session.add(change)
    session.flush()  # the messages refer to it
    signers = [(kind, value, APPROVE) for kind, value in current.items()] + [
        (kind, value, ACCEPT) for kind, value in wanted.items() if current.get(kind) != value
    ]
    for kind, value, signer_role in signers:
        nonce = secrets.token_urlsafe(32)
        session.add(
            ControllerChallenge(
                id=new_id(CHALLENGE),
                identity_id=identity.id,
                owner_change_id=change.id,
                purpose=CHANGE_PURPOSE,
                controller_type=kind,
                controller_value=value,
                nonce=nonce,
                message=change_message(
                    audience=audience,
                    did=identity.public_identifier,
                    change_id=change.id,
                    signer=value,
                    signer_role=signer_role,
                    owners_now=current,
                    new_owners=wanted,
                    nonce=nonce,
                    issued_at=now,
                    expires_at=expires_at,
                ),
                expires_at=expires_at,
                created_at=now,
            )
        )
    record_audit_event(
        session,
        action=REQUESTED,
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        data={
            "change_id": change.id,
            "owners_now": current,
            "new_owners": wanted,
            "expires_at": xml_datetime(expires_at),
        },
    )
    session.flush()
    return change


def find_change(session: Session, change_id: str, *, lock: bool = False) -> OwnerChange:
    change = session.get(OwnerChange, change_id, with_for_update=lock)
    if change is None:
        raise ApiError(404, "unknown_owner_change", "Unknown owner change.")
    return change


def messages(session: Session, change: OwnerChange) -> list[ControllerChallenge]:
    return list(
        session.scalars(
            select(ControllerChallenge)
            .where(ControllerChallenge.owner_change_id == change.id)
            .order_by(ControllerChallenge.id)
        )
    )


def _verified(session: Session, change: OwnerChange) -> list[ControllerChallenge]:
    return list(
        session.scalars(
            select(ControllerChallenge)
            .join(ControllerProof, ControllerProof.challenge_id == ControllerChallenge.id)
            .where(
                ControllerChallenge.owner_change_id == change.id,
                ControllerProof.status == "verified",
            )
        )
    )


def change_status(change: OwnerChange, now: datetime) -> str:
    if change.status == "open" and change.expires_at <= now:
        return "expired"
    return change.status


def message_status(session: Session, challenge: ControllerChallenge, now: datetime) -> str:
    if challenge.used_at is not None:
        proof = session.scalar(
            select(ControllerProof).where(ControllerProof.challenge_id == challenge.id)
        )
        return proof.status if proof is not None else "used"
    return "expired" if challenge.expires_at <= now else "open"


def still_needed(session: Session, change: OwnerChange) -> dict[str, Any]:
    """What the change still waits for: an approval by a current owner, and which new owners
    have not accepted yet."""
    signed = _verified(session, change)
    approved = any(role(change, c.controller_type, c.controller_value) == APPROVE for c in signed)
    previous = previous_owners(change)
    accepted = {(c.controller_type, c.controller_value) for c in signed}
    to_accept = [
        value
        for kind, value in new_owners(change).items()
        if previous.get(kind) != value and (kind, value) not in accepted
    ]
    return {"approval": not approved, "acceptances": to_accept}


@dataclass(frozen=True)
class SignatureOutcome:
    proof: ControllerProof
    verified: bool
    change: OwnerChange
    identity: AgentIdentity
    applied: bool


def submit_signature(
    session: Session,
    identity_text: str,
    change_id: str,
    *,
    challenge_id: str,
    signature: str,
    now: datetime,
    wallets: WalletVerifier | None = None,
) -> SignatureOutcome:
    """Check one owner's signature; once a current owner approved and every new owner
    accepted, the change applies. The caller commits even when the signature is rejected."""
    # Lock order (identity, change, message) as in create_change.
    identity = public_identity(session, identity_text, lock=True)
    if identity is None:
        raise ApiError(404, "not_found", "Unknown DID-LLM.")
    change = find_change(session, change_id, lock=True)
    challenge = session.get(ControllerChallenge, challenge_id, with_for_update=True)
    if (
        change.identity_id != identity.id
        or challenge is None
        or challenge.owner_change_id != change.id
        or challenge.purpose != CHANGE_PURPOSE
    ):
        raise ApiError(404, "unknown_challenge", "Unknown message for this owner change.")
    status = change_status(change, now)
    if status != "open":
        raise ApiError(
            410 if status == "expired" else 409,
            "owner_change_closed",
            f"This owner change is {status}; request a new one.",
        )
    if identity.status != "active":
        raise ApiError(
            409, "identity_not_active", f"This DID-LLM is {identity.status}: nothing changes."
        )
    if challenge.used_at is not None:
        raise ApiError(
            409,
            "challenge_already_used",
            "This message was already answered; request a new change.",
        )
    if challenge.expires_at <= now:
        raise ApiError(410, "challenge_expired", "This message has expired; request a new change.")

    signer_role = role(change, challenge.controller_type, challenge.controller_value)
    # An approval counts only from an owner the DID-LLM still has; a new owner's acceptance
    # is checked against its own key or wallet.
    still = (
        controllers(identity).get(challenge.controller_type) == challenge.controller_value
        if signer_role == APPROVE
        else True
    )
    check = check_signature(
        challenge.controller_type,
        challenge.controller_value,
        challenge.message,
        signature,
        still_controller=still,
        wallets=wallets,
    )
    reason = None
    if not still:
        reason = "the DID-LLM's owners changed after this message was issued"
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
    session.flush()
    record_audit_event(
        session,
        action="controller_proof.verified" if check.verified else "controller_proof.rejected",
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        data={
            "purpose": CHANGE_PURPOSE,
            "change_id": change.id,
            "role": signer_role,
            "challenge_id": challenge.id,
            "proof_id": proof.id,
            "method": check.method,
            "controller": challenge.controller_value,
            **({"reason": reason} if reason else {}),
        },
    )
    applied = False
    if check.verified:
        needed = still_needed(session, change)
        if not needed["approval"] and not needed["acceptances"]:
            _apply(session, identity, change, now)
            applied = True
    return SignatureOutcome(proof, check.verified, change, identity, applied)


def _apply(session: Session, identity: AgentIdentity, change: OwnerChange, now: datetime) -> None:
    before = controllers(identity)
    identity.controller_public_key = change.new_public_key
    identity.controller_wallet_address = change.new_wallet_address
    identity.controller_network = change.new_network
    identity.controller_changed_at = now
    identity.updated_at = now
    change.status = "applied"
    change.applied_at = now
    others = session.scalars(
        select(OwnerChange).where(
            OwnerChange.identity_id == identity.id,
            OwnerChange.status == "open",
            OwnerChange.id != change.id,
        )
    ).all()
    for other in others:
        other.status = "cancelled"
    # Apps signed in before the change sign in again (their access tokens and grants from
    # before it are refused too: api/mcp_auth.py, signer/tokens.py).
    ended = session.execute(
        update(OAuthRefreshToken)
        .where(OAuthRefreshToken.identity_id == identity.id, OAuthRefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    ).rowcount
    record_audit_event(
        session,
        action=OWNER_CHANGED,
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        data={
            "did": identity.public_identifier,
            "change_id": change.id,
            "from": before,
            "to": controllers(identity),
            "cancelled_changes": [other.id for other in others],
            "refresh_tokens_revoked": int(ended or 0),
        },
    )
    session.flush()


# --------------------------------------------------------------------------- revocation


def create_revocation(
    session: Session,
    identity_text: str,
    *,
    controller_type: str | None,
    reason: str | None,
    audience: str,
    now: datetime,
) -> ControllerChallenge:
    """The message an owner signs to revoke the DID-LLM for good."""
    now = now.replace(microsecond=0)
    identity = public_identity(session, identity_text, lock=True)
    if identity is None:
        raise ApiError(404, "not_found", "Unknown DID-LLM.")
    if identity.status == "revoked":
        raise ApiError(409, "identity_not_active", "This DID-LLM is already revoked.")
    text = clean_reason(reason)
    found = controllers(identity)
    kind = controller_type or (ED25519 if ED25519 in found else next(iter(found)))
    if kind not in found:
        raise ApiError(
            409,
            "no_such_controller",
            f"This DID-LLM has no {kind} owner; it has: " + ", ".join(found),
        )
    _make_room(session, identity.id, REVOKE_PURPOSE, now)
    nonce = secrets.token_urlsafe(32)
    expires_at = now + TTL
    challenge = ControllerChallenge(
        id=new_id(CHALLENGE),
        identity_id=identity.id,
        purpose=REVOKE_PURPOSE,
        controller_type=kind,
        controller_value=found[kind],
        nonce=nonce,
        message=revocation_message(
            audience=audience,
            did=identity.public_identifier,
            controller=found[kind],
            reason=text,
            nonce=nonce,
            issued_at=now,
            expires_at=expires_at,
        ),
        expires_at=expires_at,
        created_at=now,
    )
    session.add(challenge)
    record_audit_event(
        session,
        action=REVOCATION_REQUESTED,
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        data={"challenge_id": challenge.id, "controller_type": kind},
    )
    session.flush()
    return challenge


def find_revocation(session: Session, challenge_id: str) -> tuple[ControllerChallenge, str]:
    challenge = session.get(ControllerChallenge, challenge_id)
    if challenge is None or challenge.purpose != REVOKE_PURPOSE or challenge.identity_id is None:
        raise ApiError(404, "unknown_challenge", "Unknown revocation message.")
    identity = session.get(AgentIdentity, challenge.identity_id)
    assert identity is not None
    return challenge, identity.public_identifier


@dataclass(frozen=True)
class RevocationOutcome:
    proof: ControllerProof
    verified: bool
    identity: AgentIdentity


def submit_revocation(
    session: Session,
    identity_text: str,
    *,
    challenge_id: str,
    signature: str,
    now: datetime,
    wallets: WalletVerifier | None = None,
) -> RevocationOutcome:
    identity = public_identity(session, identity_text, lock=True)
    if identity is None:
        raise ApiError(404, "not_found", "Unknown DID-LLM.")
    challenge = session.get(ControllerChallenge, challenge_id, with_for_update=True)
    if (
        challenge is None
        or challenge.purpose != REVOKE_PURPOSE
        or challenge.identity_id != identity.id
    ):
        raise ApiError(404, "unknown_challenge", "Unknown revocation message for this DID-LLM.")
    if challenge.used_at is not None:
        raise ApiError(
            409, "challenge_already_used", "This message was already answered; request a new one."
        )
    if challenge.expires_at <= now:
        raise ApiError(410, "challenge_expired", "This message has expired; request a new one.")
    if identity.status == "revoked":
        raise ApiError(409, "identity_not_active", "This DID-LLM is already revoked.")
    still = controllers(identity).get(challenge.controller_type) == challenge.controller_value
    check = check_signature(
        challenge.controller_type,
        challenge.controller_value,
        challenge.message,
        signature,
        still_controller=still,
        wallets=wallets,
    )
    reason = None
    if not still:
        reason = "the DID-LLM's owners changed after this message was issued"
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
    session.flush()
    record_audit_event(
        session,
        action="controller_proof.verified" if check.verified else "controller_proof.rejected",
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        data={
            "purpose": REVOKE_PURPOSE,
            "challenge_id": challenge.id,
            "proof_id": proof.id,
            "method": check.method,
            "controller": challenge.controller_value,
            **({"reason": reason} if reason else {}),
        },
    )
    if check.verified:
        try:
            set_status(
                session,
                identity,
                "revoked",
                reason=_reason_in(challenge.message),
                actor_type="agent",
                actor_id=challenge.controller_value,
                now=now,
            )
        except StatusChangeError as exc:  # pragma: no cover - checked above, under the lock
            raise ApiError(409, "identity_not_active", str(exc)) from exc
        session.execute(
            update(OAuthRefreshToken)
            .where(
                OAuthRefreshToken.identity_id == identity.id,
                OAuthRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    return RevocationOutcome(proof, check.verified, identity)
