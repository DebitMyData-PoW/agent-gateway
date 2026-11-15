"""Verifying an agent by its DID-LLM (the discovery runbook's ``verify_agent``): any agent or
service checks a DID-LLM it meets, and whether the party in front of it really controls it.

    POST /v1/dids/verifications  {"did": "...", "audience": "my-service"}
         -> the DID-LLM's status, its signed credential checked against DebitMyData's issuer
            key, its owner key and/or wallet, and a challenge: a message for the other party
            to sign, valid 10 minutes
    POST /v1/dids/verifications  {"did": "...", "message": "<that message>", "signature": "..."}
         -> the same, and whether the signature is the DID-LLM's owner's now

The challenge is the verifier's, not stored here: it names the DID-LLM, who asks (``To:``) and a
random nonce. The verifier keeps the nonce it got and checks that the answer gives it back
(``proof.nonce``): that is what makes a signature fresh and meant for it. Only messages in this
form are checked, so a signature made to sign in to DebitMyData (or to prove control when the
DID-LLM was registered) is never accepted as a proof for someone else, and an agent asked to
prove control signs only a "DebitMyData proof of control" message.

``verified`` is true when the DID-LLM was issued here, is active, its credential's signature is
DebitMyData's and, when a signature was sent, it is the owner's. A smart wallet's signature is
checked on its chain where the gateway can ask it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmd_gateway.db.models import AgentIdentity, Credential
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import verify_proof, xml_datetime
from dmd_gateway.identity.issuer import Issuer
from dmd_gateway.identity.wallets import WalletVerifier
from dmd_gateway.oauth.grants import GrantRefused, message_time
from dmd_gateway.services.controller_proofs import ED25519, EIP155, check_signature
from dmd_gateway.services.directory import controllers
from dmd_gateway.services.identity_status import public_identity
from dmd_gateway.services.registrations import active_entitlements, credential_url

TITLE = "DebitMyData proof of control"
PROOF_MINUTES = 10
CLOCK_SKEW = timedelta(seconds=60)
MAX_AUDIENCE = 100
DEFAULT_AUDIENCE = "a party verifying this DID-LLM"
CONTROLLER_NAMES = {ED25519: "key", EIP155: "wallet"}
_FIELDS = ("DID-LLM", "To", "Nonce", "Issued", "Expires")


@dataclass(frozen=True)
class ProofMessage:
    did: str
    audience: str
    nonce: str
    issued: datetime
    expires: datetime


def clean_audience(text: str | None) -> str:
    """Who asks, on one line: no control characters, at most 100 characters."""
    words = " ".join("".join(c if c.isprintable() else " " for c in text or "").split())
    return words[:MAX_AUDIENCE] or DEFAULT_AUDIENCE


def challenge(did: str, audience: str | None, now: datetime) -> tuple[str, ProofMessage]:
    now = now.replace(microsecond=0)
    proof = ProofMessage(
        did=did,
        audience=clean_audience(audience),
        nonce=secrets.token_urlsafe(18),
        issued=now,
        expires=now + timedelta(minutes=PROOF_MINUTES),
    )
    text = "\n".join(
        [
            TITLE,
            f"DID-LLM: {proof.did}",
            f"To: {proof.audience}",
            f"Nonce: {proof.nonce}",
            f"Issued: {xml_datetime(proof.issued)}",
            f"Expires: {xml_datetime(proof.expires)}",
        ]
    )
    return text, proof


def read_proof(text: str, did: str, now: datetime) -> ProofMessage:
    """The challenge, if it is a proof-of-control message for ``did`` still valid; otherwise
    ``GrantRefused`` with what is wrong."""
    lines = text.replace("\r\n", "\n").split("\n")
    if len(lines) != len(_FIELDS) + 1 or lines[0] != TITLE:
        raise GrantRefused(f"not a {TITLE} message: ask for a challenge first")
    values: dict[str, str] = {}
    for name, line in zip(_FIELDS, lines[1:], strict=True):
        prefix = f"{name}: "
        if not line.startswith(prefix) or not line[len(prefix) :].strip():
            raise GrantRefused(f"the message's line {name!r} is missing")
        values[name] = line[len(prefix) :]
    proof = ProofMessage(
        did=values["DID-LLM"],
        audience=values["To"],
        nonce=values["Nonce"],
        issued=message_time(values["Issued"]),
        expires=message_time(values["Expires"]),
    )
    if proof.did != did:
        raise GrantRefused("the message is for another DID-LLM")
    if proof.issued > now + CLOCK_SKEW:
        raise GrantRefused("the message was issued in the future")
    if proof.expires < now - CLOCK_SKEW:
        raise GrantRefused("the message has expired: ask for a new challenge")
    if proof.expires - proof.issued > timedelta(minutes=PROOF_MINUTES) + CLOCK_SKEW:
        raise GrantRefused(f"a challenge lasts {PROOF_MINUTES} minutes at most")
    return proof


def _credential(
    session: Session, identity: AgentIdentity, issuer: Issuer | None, base: str
) -> dict[str, Any] | None:
    record = session.scalars(
        select(Credential)
        .where(Credential.identity_id == identity.id)
        .order_by(Credential.issued_at.desc())
    ).first()
    if record is None:
        return None
    payload = record.payload if isinstance(record.payload, dict) else {}
    proof = payload.get("proof") if isinstance(payload.get("proof"), dict) else {}
    subject = payload.get("credentialSubject")
    subject_id = subject.get("id") if isinstance(subject, dict) else None
    valid = (
        issuer is not None
        and proof.get("verificationMethod") == issuer.verification_method
        and subject_id in (None, identity.public_identifier)
        and verify_proof(payload, issuer.public_key_multibase)
    )
    status = "revoked" if record.revoked_at else identity.status
    return {
        "credential_id": record.id,
        "url": credential_url(base, record.id),
        "status": status,
        "issued_at": record.issued_at,
        "signature_valid": bool(valid),
    }


def _proof(
    identity: AgentIdentity,
    message: str,
    signature: str,
    *,
    wallets: WalletVerifier | None,
    now: datetime,
) -> dict[str, Any]:
    try:
        proof = read_proof(message, identity.public_identifier, now)
    except GrantRefused as exc:
        return {"signature_valid": False, "reason": str(exc)}
    answer: dict[str, Any] = {
        "signature_valid": False,
        "audience": proof.audience,
        "nonce": proof.nonce,
        "expires_at": proof.expires,
    }
    unreadable: list[str] = []
    for kind, value in controllers(identity).items():
        try:
            check = check_signature(
                kind, value, message, signature, still_controller=True, wallets=wallets
            )
        except ApiError as exc:
            if exc.code == "wallet_check_unavailable":
                raise
            unreadable.append(exc.message)
            continue
        if check.verified:
            return {
                **answer,
                "signature_valid": True,
                "signed_with": CONTROLLER_NAMES[kind],
                "method": check.method,
            }
    answer["reason"] = (
        "the signature is not the DID-LLM owner's key's or wallet's"
        if len(unreadable) < len(controllers(identity))
        else f"the signature cannot be read: {unreadable[0]}"
    )
    return answer


def verify_agent(
    session: Session,
    did: str,
    *,
    issuer: Issuer | None,
    base: str,
    wallets: WalletVerifier | None,
    now: datetime,
    audience: str | None = None,
    message: str | None = None,
    signature: str | None = None,
) -> dict[str, Any]:
    identity = public_identity(session, did)
    if identity is None:
        return {
            "did": did,
            "verified": False,
            "found": False,
            "reasons": ["not a DID-LLM issued by DebitMyData"],
            "checked_at": now,
        }
    reasons: list[str] = []
    if identity.status != "active":
        reasons.append(f"the DID-LLM is {identity.status}")
    credential = _credential(session, identity, issuer, base)
    if credential is None or not credential["signature_valid"]:
        reasons.append("its credential's signature is not DebitMyData's")
    elif credential["status"] != "active":
        reasons.append(f"its credential is {credential['status']}")
    result: dict[str, Any] = {
        "did": identity.public_identifier,
        "found": True,
        "status": identity.status,
        "display_name": identity.display_name,
        "participant_type": identity.participant_type,
        "issued_at": identity.created_at,
        "issuer": issuer.did if issuer is not None else None,
        "credential": credential,
        "controllers": [
            {"type": CONTROLLER_NAMES[kind], "value": value}
            for kind, value in controllers(identity).items()
        ],
        "entitlements": active_entitlements(session, identity.id),
        "checked_at": now,
    }
    if message is not None or signature is not None:
        if not message or not signature:
            raise ApiError(
                422, "invalid_request", "send both the challenge message and its signature"
            )
        result["proof"] = _proof(identity, message, signature, wallets=wallets, now=now)
        if not result["proof"]["signature_valid"]:
            reasons.append(result["proof"]["reason"])
    else:
        text, proof = challenge(identity.public_identifier, audience, now)
        result["challenge"] = {
            "message": text,
            "nonce": proof.nonce,
            "expires_at": proof.expires,
            "next": "Have the agent sign this exact message with its DID-LLM's key or wallet, "
            "then send did, message and signature here. Keep the nonce: the answer gives it "
            "back, and it must be the one you sent.",
        }
    result["verified"] = not reasons
    result["reasons"] = reasons
    return result
