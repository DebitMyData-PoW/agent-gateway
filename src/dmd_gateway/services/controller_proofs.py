"""Controller challenges and proofs (ID-03, ID-04, ID-08; directive tool dmd_prove_wallet_control).

The agent proves it holds the private key of the controller public key named in its quote:

1. it asks for a challenge: a one-time message with a random nonce that expires quickly;
2. it signs that exact message with its Ed25519 key and sends the signature back.

Every answer is recorded (append-only), and a challenge can be answered only once, whether
the signature was right or not, so a challenge can never be replayed or brute-forced.
Proofs can be completed before or after the payment settles.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import ControllerChallenge, ControllerProof, RegistrationIntent
from dmd_gateway.domain.ids import CHALLENGE, PROOF, new_id
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import (
    ControllerKey,
    ControllerKeyError,
    EncodingError,
    decode_signature,
    encode_signature,
    parse_controller_key,
    xml_datetime,
)

PURPOSE = "registration"
PROOF_METHOD = "ed25519_signature"
MAX_OPEN_CHALLENGES = 5
CLOSED_STATUSES = frozenset({"issued", "expired", "rejected", "cancelled"})


def challenge_message(
    *,
    audience: str,
    registration_intent_id: str,
    controller: str,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    """The exact text the agent signs (UTF-8, lines joined by \\n, no trailing newline)."""
    return "\n".join(
        [
            "DebitMyData controller proof",
            f"Audience: {audience}",
            "Purpose: did_registration",
            f"Registration: {registration_intent_id}",
            f"Controller: {controller}",
            f"Nonce: {nonce}",
            f"Issued: {xml_datetime(issued_at)}",
            f"Expires: {xml_datetime(expires_at)}",
        ]
    )


def _open_registration(session: Session, registration_intent_id: str) -> RegistrationIntent:
    registration = session.get(RegistrationIntent, registration_intent_id, with_for_update=True)
    if registration is None:
        raise ApiError(404, "not_found", "Unknown registration intent.")
    if registration.status == "issued":
        raise ApiError(409, "already_issued", "The DID for this registration was already issued.")
    if registration.status in CLOSED_STATUSES:
        raise ApiError(
            409,
            "registration_closed",
            f"This registration is {registration.status}; request a new quote.",
        )
    return registration


def _controller_key(registration: RegistrationIntent) -> ControllerKey:
    if not registration.controller_public_key:
        raise ApiError(
            409,
            "no_controller_key",
            "This registration has no controller public key; request a new quote with one.",
        )
    try:
        return parse_controller_key(registration.controller_public_key)
    except ControllerKeyError as exc:
        raise ApiError(
            409,
            "invalid_controller_key",
            "This registration's controller key is not a usable ed25519 public key; "
            "request a new quote.",
        ) from exc


def create_challenge(
    session: Session,
    registration_intent_id: str,
    *,
    audience: str,
    ttl: timedelta,
    now: datetime,
) -> ControllerChallenge:
    registration = _open_registration(session, registration_intent_id)
    key = _controller_key(registration)

    open_challenges = session.scalar(
        select(func.count())
        .select_from(ControllerChallenge)
        .where(
            ControllerChallenge.registration_intent_id == registration.id,
            ControllerChallenge.used_at.is_(None),
            ControllerChallenge.expires_at > now,
        )
    )
    if open_challenges >= MAX_OPEN_CHALLENGES:
        raise ApiError(
            429,
            "too_many_challenges",
            "Too many open challenges for this registration; answer one or wait for them "
            "to expire.",
            headers={"Retry-After": str(int(ttl.total_seconds()))},
        )

    nonce = secrets.token_urlsafe(32)
    expires_at = now + ttl
    challenge = ControllerChallenge(
        id=new_id(CHALLENGE),
        registration_intent_id=registration.id,
        purpose=PURPOSE,
        controller_type=key.key_type,
        controller_value=key.canonical,
        nonce=nonce,
        message=challenge_message(
            audience=audience,
            registration_intent_id=registration.id,
            controller=key.canonical,
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
        action="controller_challenge.created",
        subject_type="registration_intent",
        subject_id=registration.id,
        actor_type="agent",
        data={"challenge_id": challenge.id, "expires_at": xml_datetime(expires_at)},
    )
    return challenge


@dataclass(frozen=True)
class ProofOutcome:
    proof: ControllerProof
    verified: bool


def submit_proof(
    session: Session,
    registration_intent_id: str,
    *,
    challenge_id: str,
    signature: str,
    now: datetime,
) -> ProofOutcome:
    """Check a signed challenge. The caller commits even when the proof is rejected."""
    # Lock order (registration, then challenge) matches create_challenge.
    registration = _open_registration(session, registration_intent_id)
    challenge = session.get(ControllerChallenge, challenge_id, with_for_update=True)
    if challenge is None or challenge.registration_intent_id != registration.id:
        raise ApiError(404, "unknown_challenge", "Unknown challenge for this registration.")
    if challenge.used_at is not None:
        raise ApiError(
            409,
            "challenge_already_used",
            "This challenge was already answered; request a new one.",
        )
    if challenge.expires_at <= now:
        raise ApiError(410, "challenge_expired", "This challenge has expired; request a new one.")
    try:
        signature_bytes = decode_signature(signature)
    except EncodingError as exc:
        # A malformed value is a client mistake, not a wrong answer: the challenge stays open.
        raise ApiError(422, "invalid_signature_encoding", str(exc)) from exc

    key = parse_controller_key(challenge.controller_value)
    still_controller = registration.controller_public_key == challenge.controller_value
    verified = still_controller and key.verify(signature_bytes, challenge.message.encode("utf-8"))
    reason = None
    if not still_controller:
        reason = "the registration's controller changed after this challenge was issued"
    elif not verified:
        reason = "the signature does not match the controller public key"

    challenge.used_at = now
    proof = ControllerProof(
        id=new_id(PROOF),
        challenge_id=challenge.id,
        method=PROOF_METHOD,
        signature=encode_signature(signature_bytes),
        status="verified" if verified else "rejected",
        verified_at=now if verified else None,
        failure_reason=reason,
        created_at=now,
    )
    session.add(proof)
    record_audit_event(
        session,
        action="controller_proof.verified" if verified else "controller_proof.rejected",
        subject_type="registration_intent",
        subject_id=registration.id,
        actor_type="agent",
        data={
            "challenge_id": challenge.id,
            "proof_id": proof.id,
            "method": PROOF_METHOD,
            "controller": challenge.controller_value,
            **({"reason": reason} if reason else {}),
        },
    )
    return ProofOutcome(proof=proof, verified=verified)


def verified_proof(session: Session, registration: RegistrationIntent) -> ControllerProof | None:
    """The first verified proof for the registration's current controller key, if any."""
    if not registration.controller_public_key:
        return None
    return session.scalars(
        select(ControllerProof)
        .join(ControllerChallenge, ControllerChallenge.id == ControllerProof.challenge_id)
        .where(
            ControllerChallenge.registration_intent_id == registration.id,
            ControllerChallenge.purpose == PURPOSE,
            ControllerChallenge.controller_value == registration.controller_public_key,
            ControllerProof.status == "verified",
        )
        .order_by(ControllerProof.verified_at)
        .limit(1)
    ).first()
