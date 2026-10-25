"""Controller challenges and proofs (ID-03, ID-04, ID-06, ID-08; directive tool
dmd_prove_wallet_control).

The agent proves it controls each controller named in its quote, an Ed25519 public key
and/or an EVM wallet:

1. it asks for a challenge: a one-time message with a random nonce that expires quickly;
2. it signs that exact message, with its Ed25519 key or with its wallet (EIP-191, the way
   wallets sign text; smart-contract wallets through ERC-1271 or ERC-6492, see
   ``identity/wallets.py``), and sends the signature back.

Every answer is recorded (append-only), and a challenge can be answered only once, whether
the signature was right or not, so a challenge can never be replayed or brute-forced.
Proofs can be completed before or after the payment settles.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
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
from dmd_gateway.identity.wallets import (
    SmartWalletUnsupported,
    WalletCheckUnavailable,
    WalletError,
    WalletVerifier,
    decode_wallet_signature,
    wallet_from_account_id,
)

PURPOSE = "registration"
PROOF_METHOD = "ed25519_signature"
WALLET_REJECTED_METHOD = "wallet_signature"
ED25519 = "ed25519"
EIP155 = "eip155"
CONTROLLER_TYPES = (ED25519, EIP155)
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


def declared_controllers(registration: RegistrationIntent) -> dict[str, str]:
    """Controller type -> value (``ed25519:z6Mk...``, ``eip155:<chain id>:0x...``), each of
    which must prove control before the DID is issued."""
    controllers: dict[str, str] = {}
    if registration.controller_public_key:
        controllers[ED25519] = registration.controller_public_key
    if registration.controller_wallet_address and registration.controller_network:
        controllers[EIP155] = (
            f"{registration.controller_network}:{registration.controller_wallet_address}"
        )
    return controllers


def _controller_key(value: str) -> ControllerKey:
    try:
        return parse_controller_key(value)
    except ControllerKeyError as exc:
        raise ApiError(
            409,
            "invalid_controller_key",
            "This registration's controller key is not a usable ed25519 public key; "
            "request a new quote.",
        ) from exc


def _choose_controller(
    session: Session, registration: RegistrationIntent, controller_type: str | None
) -> tuple[str, str]:
    declared = declared_controllers(registration)
    if not declared:
        raise ApiError(
            409,
            "no_controller",
            "This registration has no controller; request a new quote with one.",
        )
    if controller_type is not None:
        if controller_type not in declared:
            raise ApiError(
                409,
                "no_such_controller",
                f"This registration has no {controller_type} controller; it has: "
                + ", ".join(declared),
            )
        return controller_type, declared[controller_type]
    # Not said: the only controller, or the first one still to prove.
    proven = verified_proofs(session, registration)
    for kind, value in declared.items():
        if kind not in proven:
            return kind, value
    kind = next(iter(declared))
    return kind, declared[kind]


def create_challenge(
    session: Session,
    registration_intent_id: str,
    *,
    controller_type: str | None = None,
    audience: str,
    ttl: timedelta,
    now: datetime,
) -> ControllerChallenge:
    registration = _open_registration(session, registration_intent_id)
    kind, controller = _choose_controller(session, registration, controller_type)
    if kind == ED25519:
        controller = _controller_key(controller).canonical

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
        controller_type=kind,
        controller_value=controller,
        nonce=nonce,
        message=challenge_message(
            audience=audience,
            registration_intent_id=registration.id,
            controller=controller,
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
        data={
            "challenge_id": challenge.id,
            "controller_type": kind,
            "expires_at": xml_datetime(expires_at),
        },
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
    wallets: WalletVerifier | None = None,
) -> ProofOutcome:
    """Check a signed challenge. The caller commits even when the proof is rejected.

    A malformed signature, or a smart-wallet check the wallet's network cannot answer, leaves
    the challenge open: neither is a wrong answer.
    """
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

    current = declared_controllers(registration).get(challenge.controller_type)
    still_controller = current == challenge.controller_value
    message = challenge.message
    if challenge.controller_type == EIP155:
        try:
            signature_bytes = decode_wallet_signature(signature)
        except WalletError as exc:
            raise ApiError(422, "invalid_signature_encoding", str(exc)) from exc
        method: str | None = None
        if still_controller:
            if wallets is None:
                raise ApiError(
                    503, "wallet_check_unavailable", "Wallet proofs are not available here."
                )
            try:
                method = wallets.verify(
                    wallet_from_account_id(challenge.controller_value), message, signature_bytes
                )
            except WalletCheckUnavailable as exc:
                raise ApiError(
                    503,
                    "wallet_check_unavailable",
                    f"The wallet's network could not be asked ({exc}); the challenge is still "
                    "open, try again shortly.",
                    headers={"Retry-After": "30"},
                ) from exc
            except SmartWalletUnsupported as exc:
                raise ApiError(
                    422,
                    "smart_wallet_not_supported",
                    f"This is not an ordinary wallet's signature for this address, and {exc}. "
                    "The challenge is still open: sign it with the wallet's own key, or "
                    "request a new quote with a wallet on another network.",
                ) from exc
        verified = method is not None
        proof_method = method or WALLET_REJECTED_METHOD
        stored_signature = "0x" + signature_bytes.hex()
        what = "the controller wallet"
    else:
        try:
            signature_bytes = decode_signature(signature)
        except EncodingError as exc:
            raise ApiError(422, "invalid_signature_encoding", str(exc)) from exc
        key = parse_controller_key(challenge.controller_value)
        verified = still_controller and key.verify(signature_bytes, message.encode("utf-8"))
        proof_method = PROOF_METHOD
        stored_signature = encode_signature(signature_bytes)
        what = "the controller public key"

    reason = None
    if not still_controller:
        reason = "the registration's controller changed after this challenge was issued"
    elif not verified:
        reason = f"the signature does not match {what}"

    challenge.used_at = now
    proof = ControllerProof(
        id=new_id(PROOF),
        challenge_id=challenge.id,
        method=proof_method,
        signature=stored_signature,
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
            "method": proof_method,
            "controller": challenge.controller_value,
            **({"reason": reason} if reason else {}),
        },
    )
    return ProofOutcome(proof=proof, verified=verified)


def verified_proofs(
    session: Session, registration: RegistrationIntent
) -> Mapping[str, ControllerProof]:
    """Controller type -> the first verified proof for that controller, as currently named
    in the registration. Every declared controller needs one before the DID is issued."""
    declared = declared_controllers(registration)
    if not declared:
        return {}
    rows = session.execute(
        select(
            ControllerChallenge.controller_type,
            ControllerChallenge.controller_value,
            ControllerProof,
        )
        .join(ControllerProof, ControllerProof.challenge_id == ControllerChallenge.id)
        .where(
            ControllerChallenge.registration_intent_id == registration.id,
            ControllerChallenge.purpose == PURPOSE,
            ControllerProof.status == "verified",
        )
        .order_by(ControllerProof.verified_at)
    ).all()
    proofs: dict[str, ControllerProof] = {}
    for kind, value, proof in rows:
        if declared.get(kind) == value and kind not in proofs:
            proofs[kind] = proof
    return proofs
