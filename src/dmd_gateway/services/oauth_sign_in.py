"""Signing in to an app with a DID-LLM, in the browser (OAuth phase 2): the authorization code
flow with PKCE (OAuth 2.1), where the DID-LLM's owner proves it by signing one message with
the DID-LLM's own wallet or key, as when it was issued.

    start        an app's authorization request, checked (api/oauth_browser.py)
    choose       the person names their DID-LLM: the message to sign, for its controller
    approve      their signature, checked like a controller proof (5 tries)
    finish       an authorization code for the app, once, valid one minute
    exchange     the app trades the code (with its PKCE verifier) for tokens
    refresh      a refresh token for new tokens: used once, replay revokes the whole family
    revoke       the app (or the person, through it) ends the sign-in

Codes and refresh tokens are kept as SHA-256 hashes only. A suspended or revoked DID-LLM can
neither sign in nor refresh.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import Session

from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import AgentIdentity, OAuthCode, OAuthRefreshToken, OAuthSignIn
from dmd_gateway.domain.ids import new_id
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import xml_datetime
from dmd_gateway.identity.wallets import WalletVerifier
from dmd_gateway.oauth.clients import Client
from dmd_gateway.oauth.tokens import SCOPES
from dmd_gateway.services.controller_proofs import (
    ED25519,
    EIP155,
    SignatureCheck,
    check_signature,
)
from dmd_gateway.services.directory import controllers
from dmd_gateway.services.identity_status import public_identity

SIGN_IN = "dmd_signin"
SIGN_IN_MINUTES = 15
CODE_SECONDS = 60
REFRESH_DAYS = 30
MAX_ATTEMPTS = 5
VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
CONTROLLER_LABELS = {EIP155: "wallet", ED25519: "key"}


class TokenError(Exception):
    """A refused token request: RFC 6749 section 5.2 ``error`` and description."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class AuthorizationRequest:
    client: Client
    redirect_uri: str
    state: str | None
    code_challenge: str
    scopes: tuple[str, ...]
    resource: str


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_matches(verifier: str, challenge: str) -> bool:
    if not VERIFIER.fullmatch(verifier):
        return False
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return secrets.compare_digest(computed.rstrip(b"=").decode("ascii"), challenge)


def scope_words(scope: str) -> str:
    return "; ".join(SCOPES.get(name, name) for name in scope.split())


# --------------------------------------------------------------------------- the browser part


def forget_old(session: Session, now: datetime) -> None:
    """What can no longer be used is deleted a day later (sign-ins, codes, refresh tokens)."""
    before = now - timedelta(days=1)
    session.execute(delete(OAuthCode).where(OAuthCode.expires_at < before))
    session.execute(delete(OAuthRefreshToken).where(OAuthRefreshToken.expires_at < before))
    session.execute(
        delete(OAuthSignIn).where(
            OAuthSignIn.expires_at < before,
            ~exists().where(OAuthCode.sign_in_id == OAuthSignIn.id),
        )
    )


def start(session: Session, request: AuthorizationRequest, now: datetime) -> OAuthSignIn:
    forget_old(session, now)
    sign_in = OAuthSignIn(
        id=new_id(SIGN_IN),
        client_id=request.client.client_id,
        client_name=f"{request.client.name} ({request.client.host})",
        redirect_uri=request.redirect_uri,
        state=request.state,
        code_challenge=request.code_challenge,
        scope=" ".join(request.scopes),
        resource=request.resource,
        attempts=0,
        status="open",
        expires_at=now + timedelta(minutes=SIGN_IN_MINUTES),
        created_at=now,
    )
    session.add(sign_in)
    return sign_in


def find(session: Session, sign_in_id: str, now: datetime, *, lock: bool = False) -> OAuthSignIn:
    sign_in = session.get(OAuthSignIn, sign_in_id, with_for_update=lock)
    if sign_in is None:
        raise ApiError(404, "unknown_sign_in", "Unknown sign-in: start again from the app.")
    if sign_in.status in ("open", "approved") and sign_in.expires_at <= now:
        raise ApiError(
            410, "sign_in_expired", "This sign-in has expired: start again from the app."
        )
    return sign_in


def _open(sign_in: OAuthSignIn) -> None:
    if sign_in.status != "open":
        raise ApiError(409, f"sign_in_{sign_in.status}", f"This sign-in is {sign_in.status}.")


def message(sign_in: OAuthSignIn, did: str, issuer: str, nonce: str, now: datetime) -> str:
    return "\n".join(
        [
            "DebitMyData sign-in",
            f"Sign in to: {sign_in.client_name}",
            f"DID-LLM: {did}",
            f"Allows: {scope_words(sign_in.scope)}",
            f"For: {sign_in.resource}",
            f"Sign-in server: {issuer}",
            f"Nonce: {nonce}",
            f"Issued: {xml_datetime(now)}",
            f"Expires: {xml_datetime(sign_in.expires_at)}",
        ]
    )


def choose(
    session: Session,
    sign_in: OAuthSignIn,
    did_text: str,
    controller_type: str | None,
    *,
    issuer: str,
    now: datetime,
) -> tuple[AgentIdentity, dict[str, str]]:
    """The DID-LLM the person signs in with: the message to sign, for one of its controllers
    (its wallet first, the usual way in a browser)."""
    _open(sign_in)
    identity = public_identity(session, did_text)
    if identity is None:
        raise ApiError(404, "unknown_did", "Unknown DID-LLM: check it on its page or in its app.")
    if identity.status != "active":
        raise ApiError(409, "identity_not_active", f"This DID-LLM is {identity.status}.")
    found = controllers(identity)
    if controller_type is None:
        controller_type = EIP155 if EIP155 in found else ED25519
    if controller_type not in found:
        raise ApiError(409, "no_such_controller", "This DID-LLM has no such owner key or wallet.")
    nonce = secrets.token_urlsafe(18)
    sign_in.identity_id = identity.id
    sign_in.controller_type = controller_type
    sign_in.controller_value = found[controller_type]
    sign_in.nonce = nonce
    sign_in.message = message(sign_in, identity.public_identifier, issuer, nonce, now)
    sign_in.attempts = 0
    return identity, found


def approve(
    session: Session,
    sign_in: OAuthSignIn,
    signature: str,
    *,
    wallets: WalletVerifier | None,
    now: datetime,
) -> bool:
    """Check the owner's signature of the message; five tries."""
    _open(sign_in)
    if sign_in.message is None or sign_in.identity_id is None:
        raise ApiError(409, "no_message", "Name the DID-LLM first, then sign its message.")
    if sign_in.attempts >= MAX_ATTEMPTS:
        raise ApiError(429, "too_many_attempts", "Too many wrong signatures: start again.")
    identity = session.get(AgentIdentity, sign_in.identity_id)
    assert identity is not None
    if identity.status != "active":
        raise ApiError(409, "identity_not_active", f"This DID-LLM is {identity.status}.")
    assert sign_in.controller_type is not None and sign_in.controller_value is not None
    still = controllers(identity).get(sign_in.controller_type) == sign_in.controller_value
    try:
        check = check_signature(
            sign_in.controller_type,
            sign_in.controller_value,
            sign_in.message,
            signature,
            still_controller=still,
            wallets=wallets,
        )
    except ApiError as exc:
        if exc.code != "smart_wallet_not_supported":
            raise  # a malformed signature, or the wallet's network unreachable: not an answer
        # Not this wallet's own signature, and no way to ask a smart wallet here: a wrong try.
        check = SignatureCheck(False, "smart_wallet_unchecked", signature, "the owner wallet")
    sign_in.attempts += 1
    if check.verified:
        sign_in.status = "approved"
        sign_in.approved_at = now
    record_audit_event(
        session,
        action="oauth.sign_in_approved" if check.verified else "oauth.sign_in_rejected",
        subject_type="identity",
        subject_id=identity.id,
        actor_type="agent",
        actor_id=identity.public_identifier,
        data={"sign_in_id": sign_in.id, "client_id": sign_in.client_id, "method": check.method},
    )
    return check.verified


def finish(session: Session, sign_in: OAuthSignIn, now: datetime) -> str:
    """The authorization code for the app (once): the sign-in is then finished."""
    if sign_in.status != "approved":
        raise ApiError(409, f"sign_in_{sign_in.status}", f"This sign-in is {sign_in.status}.")
    identity = session.get(AgentIdentity, sign_in.identity_id)
    assert identity is not None
    code = secrets.token_urlsafe(32)
    session.add(
        OAuthCode(
            code_hash=digest(code),
            sign_in_id=sign_in.id,
            client_id=sign_in.client_id,
            redirect_uri=sign_in.redirect_uri,
            code_challenge=sign_in.code_challenge,
            identity_id=identity.id,
            did=identity.public_identifier,
            scope=sign_in.scope,
            resource=sign_in.resource,
            expires_at=now + timedelta(seconds=CODE_SECONDS),
            created_at=now,
        )
    )
    sign_in.status = "finished"
    return code


def deny(sign_in: OAuthSignIn) -> None:
    if sign_in.status in ("open", "approved"):
        sign_in.status = "denied"


# --------------------------------------------------------------------------- the app's part


def _active(session: Session, identity_id: str) -> AgentIdentity:
    identity = session.get(AgentIdentity, identity_id)
    if identity is None or identity.status != "active":
        status = identity.status if identity is not None else "unknown"
        raise TokenError("invalid_grant", f"this DID-LLM is {status}")
    return identity


def revoke_family(session: Session, family_id: str, now: datetime) -> None:
    session.execute(
        update(OAuthRefreshToken)
        .where(OAuthRefreshToken.family_id == family_id, OAuthRefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def exchange(
    session: Session,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str | None,
    now: datetime,
) -> OAuthCode:
    row = session.get(OAuthCode, digest(code), with_for_update=True)
    if row is None:
        raise TokenError("invalid_grant", "unknown authorization code")
    if row.used_at is not None:
        # A replayed code: whatever it gave is no longer trusted (RFC 9700, section 4.5).
        if row.family_id:
            revoke_family(session, row.family_id, now)
        raise TokenError("invalid_grant", "this authorization code was already used")
    if row.expires_at <= now:
        raise TokenError("invalid_grant", "this authorization code has expired")
    if row.client_id != client_id:
        raise TokenError("invalid_grant", "this authorization code is for another app")
    if row.redirect_uri != redirect_uri:
        raise TokenError("invalid_grant", "redirect_uri must be the one of the authorization")
    if not pkce_matches(code_verifier, row.code_challenge):
        raise TokenError("invalid_grant", "code_verifier does not match the code_challenge")
    if resource and resource.rstrip("/") != row.resource:
        raise TokenError("invalid_target", "resource must be the one of the authorization")
    _active(session, row.identity_id)
    row.used_at = now
    row.family_id = new_id("dmd_oauthfam")
    return row


def new_refresh_token(
    session: Session,
    *,
    family_id: str,
    client_id: str,
    identity_id: str,
    did: str,
    scope: str,
    resource: str,
    now: datetime,
) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        OAuthRefreshToken(
            token_hash=digest(token),
            family_id=family_id,
            client_id=client_id,
            identity_id=identity_id,
            did=did,
            scope=scope,
            resource=resource,
            expires_at=now + timedelta(days=REFRESH_DAYS),
            created_at=now,
        )
    )
    return token


def use_refresh_token(
    session: Session,
    *,
    token: str,
    client_id: str,
    scope: str | None,
    resource: str | None,
    now: datetime,
) -> tuple[OAuthRefreshToken, str]:
    """The refresh token (now used) and the scope for the new tokens."""
    row = session.get(OAuthRefreshToken, digest(token), with_for_update=True)
    if row is None or row.client_id != client_id:
        raise TokenError("invalid_grant", "unknown refresh token")
    if row.revoked_at is not None:
        raise TokenError("invalid_grant", "this sign-in was ended: sign in again")
    if row.used_at is not None:
        revoke_family(session, row.family_id, now)
        raise TokenError("invalid_grant", "this refresh token was already used: sign in again")
    if row.expires_at <= now:
        raise TokenError("invalid_grant", "this refresh token has expired: sign in again")
    if resource and resource.rstrip("/") != row.resource:
        raise TokenError("invalid_target", "resource must be the one of the sign-in")
    granted = row.scope.split()
    asked = scope.split() if scope and scope.strip() else granted
    if any(name not in granted for name in asked):
        raise TokenError("invalid_scope", "a refresh can only keep or narrow the scope")
    try:
        _active(session, row.identity_id)
    except TokenError:
        revoke_family(session, row.family_id, now)
        raise
    row.used_at = now
    return row, " ".join(asked)


def revoke(session: Session, *, token: str, client_id: str, now: datetime) -> None:
    """RFC 7009: ends the sign-in the refresh token belongs to (an unknown token is no error)."""
    row = session.scalar(
        select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == digest(token))
    )
    if row is not None and row.client_id == client_id:
        revoke_family(session, row.family_id, now)


def describe(sign_in: OAuthSignIn, identity: AgentIdentity | None = None) -> dict[str, Any]:
    """What the sign-in page and the owner-key tool show."""
    controller = None
    if sign_in.controller_type is not None and sign_in.controller_value is not None:
        controller = {
            "type": CONTROLLER_LABELS.get(sign_in.controller_type, sign_in.controller_type),
            "value": sign_in.controller_value,
        }
    return {
        "sign_in_id": sign_in.id,
        "status": sign_in.status,
        "app": sign_in.client_name,
        "allows": scope_words(sign_in.scope),
        "scope": sign_in.scope,
        "resource": sign_in.resource,
        "did": identity.public_identifier if identity is not None else None,
        "controller": controller,
        "message": sign_in.message,
        "expires_at": xml_datetime(sign_in.expires_at),
    }
