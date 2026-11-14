"""What the signer checks before it signs an OAuth access token (production).

In production the token-signing key is the signer's, like the issuer key, and the signer checks
everything itself, through its read-only database role (which may read the DID-LLMs only),
before signing anything.

Agents (phase 1, ``{"op": "token", "assertion": ...}``), with the agent's signed request:

* the request is a JWT signed with the Ed25519 key that controls an active DID-LLM, for this
  authorization server (``aud``), valid for 5 minutes at most and not expired (the same checks
  as the API's, oauth/assertion.py);
* each signed request is used once: the signer remembers them until they expire (in memory;
  they expire within minutes, so a restart forgets nothing that still matters).

Agents owned by a wallet (``{"op": "token", "wallet_message": ..., "wallet_signature": ...}``):
the message from /challenge must be this sign-in server's, for one of its MCP addresses and the
scope asked, still valid and used once (oauth/wallet_sign_in.py), and the signature the
DID-LLM's owner wallet's, checked with no network (an ordinary wallet; a smart wallet cannot be
checked here, so it is refused).

People signing in to apps (phase 2), in two steps:

* ``{"op": "sign_in", ...}``, with the message the DID-LLM's owner signed in the browser and
  the signature: the message must be this sign-in server's, for this DID-LLM, app (its site),
  scope and MCP server, still valid and used once (oauth/grants.py), and the signature the
  current owner key's or ordinary wallet's (a smart wallet cannot be checked with no network,
  so it is refused). The answer is a grant, valid 30 days, that the API keeps;
* ``{"op": "token", "grant": ...}``: an access token under the grant, for the same app and MCP
  server and the same scope or less, while the DID-LLM is active.

Every token is for this gateway's MCP server only (its address, or its first address /mcp),
with known scopes, for 1 to 60 minutes. So even a fully compromised API server cannot have
the signer make a token for a DID-LLM that did not sign in itself (or whose owner did not sign
in to that app in the last 30 days), nor a token for another service.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from sqlalchemy import select
from sqlalchemy.orm import Session

from dmd_gateway.api.brand import MCP_OLD_PATHS, MCP_PATH
from dmd_gateway.db.models import AgentIdentity
from dmd_gateway.oauth import grants
from dmd_gateway.oauth import wallet_sign_in as wallet
from dmd_gateway.oauth.assertion import (
    CLOCK_SKEW_SECONDS,
    AssertionRejected,
    database_lookup,
    verify_assertion,
)
from dmd_gateway.oauth.grants import GrantRefused
from dmd_gateway.oauth.keys import public_jwk
from dmd_gateway.oauth.tokens import SCOPES, TOKEN_PATH, issue_token

MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 3600
# More signed requests than this within their few minutes of validity is not normal use.
MAX_REMEMBERED = 100_000


class TokenRefused(RuntimeError):
    """The token request does not hold up; nothing is signed."""


def resources_for(issuer_did: str) -> tuple[str, ...]:
    """The MCP server's addresses at the issuer's host (did:web:api.debitmydata.io ->
    https://api.debitmydata.io/agenticsuperhighway and .../mcp)."""
    host = issuer_did.removeprefix("did:web:").split(":", 1)[0].replace("%3A", ":")
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    return tuple(f"{scheme}://{host}{path}" for path in (MCP_PATH, *MCP_OLD_PATHS))


@dataclass
class UsedRequests:
    """The signed requests already used, until they expire."""

    seen: dict[tuple[str, str], datetime] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def use(self, did: str, jti: str, expires_at: datetime, now: datetime) -> None:
        forget_before = now - timedelta(seconds=CLOCK_SKEW_SECONDS * 2)
        with self.lock:
            for key in [key for key, until in self.seen.items() if until < forget_before]:
                del self.seen[key]
            if (did, jti) in self.seen:
                raise TokenRefused("this signed request was already used")
            if len(self.seen) >= MAX_REMEMBERED:
                raise TokenRefused("too many sign-ins at once: try again in a minute")
            self.seen[(did, jti)] = expires_at


@dataclass(frozen=True)
class TokenIssuer:
    key: RSAPrivateKey
    issuer: str
    resources: tuple[str, ...]
    used: UsedRequests = field(default_factory=UsedRequests)

    @property
    def public_jwk(self) -> dict[str, str]:
        return public_jwk(self.key.public_key())

    def _target(self, request: Mapping[str, Any]) -> tuple[str, tuple[str, ...], int]:
        """What every token request names: the MCP server, the scope, the lifetime."""
        resource, scope, ttl = (
            request.get("resource"),
            request.get("scope"),
            request.get("ttl_seconds"),
        )
        if not isinstance(resource, str):
            raise TokenRefused("a token request needs a resource")
        if not isinstance(scope, str) or not isinstance(ttl, int) or isinstance(ttl, bool):
            raise TokenRefused("a token request needs a scope and ttl_seconds")
        if resource not in self.resources:
            raise TokenRefused("the token would be for another service")
        scopes = tuple(scope.split())
        if not scopes or any(name not in SCOPES for name in scopes):
            raise TokenRefused("unknown scope")
        if not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
            raise TokenRefused("ttl_seconds must be 60 to 3600")
        return resource, scopes, ttl

    def issue(
        self, session: Session, request: Mapping[str, Any], now: datetime
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """The signed token and what it was checked against, or ``TokenRefused``."""
        if "grant" in request:
            return self._issue_for_app(session, request, now)
        if "wallet_message" in request:
            return self._issue_for_wallet(session, request, now)
        resource, scopes, ttl = self._target(request)
        assertion = request.get("assertion")
        if not isinstance(assertion, str):
            raise TokenRefused("a token request needs an assertion and a resource")
        try:
            checked = verify_assertion(
                assertion,
                audience=[f"{self.issuer}{TOKEN_PATH}", self.issuer],
                controller_key=database_lookup(session),
                now=now,
            )
        except AssertionRejected as exc:
            raise TokenRefused(exc.reason) from exc
        self.used.use(checked.did, checked.jti, checked.expires_at, now)
        issued = issue_token(
            self.key,
            issuer=self.issuer,
            did=checked.did,
            audience=resource,
            scopes=scopes,
            ttl_seconds=ttl,
            now=now,
        )
        answer = {
            "ok": True,
            "access_token": issued.access_token,
            "expires_in": issued.expires_in,
            "scope": issued.scope,
        }
        facts = {"kind": "access_token", "did": checked.did, "token_id": issued.jti}
        return answer, facts

    # ----------------------------------------------------------------------- apps (phase 2)

    def sign_in(
        self, session: Session, request: Mapping[str, Any], now: datetime
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """A grant for a person's sign-in to an app, once the owner's signature is checked."""
        fields = ("message", "signature", "did", "controller_type", "client_id", "scope")
        values = {name: request.get(name) for name in (*fields, "resource")}
        if not all(isinstance(value, str) and value for value in values.values()):
            raise TokenRefused("a sign-in needs " + ", ".join((*fields, "resource")))
        message, did = str(values["message"]), str(values["did"])
        client_id, scope = str(values["client_id"]), str(values["scope"])
        resource, controller_type = str(values["resource"]), str(values["controller_type"])
        if not client_id.startswith("https://"):
            raise TokenRefused("the app must be an https client_id")
        try:
            checked = grants.check_sign_in(
                message,
                did=did,
                client_id=client_id,
                scope=scope,
                resource=resource,
                issuer=self.issuer,
                resources=self.resources,
                now=now,
            )
        except GrantRefused as exc:
            raise TokenRefused(str(exc)) from exc
        identity = _active_identity(session, did)
        controller = _controller(identity, controller_type)
        try:
            signed = grants.owner_signed(
                controller_type, controller, message, str(values["signature"])
            )
        except GrantRefused as exc:
            raise TokenRefused(str(exc)) from exc
        if not signed:
            raise TokenRefused(
                "the signature is not the DID-LLM owner's (or a smart wallet's, which cannot "
                "be checked here)"
            )
        self.used.use("sign-in", checked.nonce, checked.expires, now)
        grant = grants.issue_grant(
            self.key,
            issuer=self.issuer,
            did=did,
            client_id=client_id,
            scope=scope,
            resource=resource,
            nonce=checked.nonce,
            now=now,
        )
        facts = {
            "kind": "sign_in_grant",
            "did": did,
            "client_id": client_id,
            "controller": controller_type,
            "grant_id": grant.jti,
        }
        return {"ok": True, "grant": grant.token}, facts

    def _issue_for_app(
        self, session: Session, request: Mapping[str, Any], now: datetime
    ) -> tuple[dict[str, Any], dict[str, str]]:
        resource, scopes, ttl = self._target(request)
        token, client_id = request.get("grant"), request.get("client_id")
        if not isinstance(token, str) or not isinstance(client_id, str):
            raise TokenRefused("a token request for an app needs its grant and client_id")
        try:
            grant = grants.verify_grant(
                token, (self.key.public_key(),), issuer=self.issuer, now=now
            )
            grants.check_use(grant, client_id=client_id, resource=resource, scopes=scopes)
        except GrantRefused as exc:
            raise TokenRefused(str(exc)) from exc
        _active_identity(session, grant.did)
        issued = issue_token(
            self.key,
            issuer=self.issuer,
            did=grant.did,
            audience=resource,
            scopes=scopes,
            ttl_seconds=ttl,
            now=now,
            client_id=client_id,
        )
        answer = {
            "ok": True,
            "access_token": issued.access_token,
            "expires_in": issued.expires_in,
            "scope": issued.scope,
        }
        facts = {
            "kind": "access_token",
            "did": grant.did,
            "client_id": client_id,
            "grant_id": grant.jti,
            "token_id": issued.jti,
        }
        return answer, facts

    def _issue_for_wallet(
        self, session: Session, request: Mapping[str, Any], now: datetime
    ) -> tuple[dict[str, Any], dict[str, str]]:
        resource, scopes, ttl = self._target(request)
        text, signature = request.get("wallet_message"), request.get("wallet_signature")
        if not isinstance(text, str) or not isinstance(signature, str):
            raise TokenRefused("a wallet sign-in needs the message and its signature")
        try:
            message, _ = wallet.check(
                text,
                issuer=self.issuer,
                resources=self.resources,
                now=now,
                scope=" ".join(scopes),
                resource=resource,
            )
            identity = _active_identity(session, message.did)
            owner = _controller(identity, grants.EIP155)
            signed = grants.owner_signed(grants.EIP155, owner, text, signature)
        except GrantRefused as exc:
            raise TokenRefused(str(exc)) from exc
        if not signed:
            raise TokenRefused(
                "the signature is not the owner wallet's (or a smart wallet's, which cannot be "
                "checked here)"
            )
        self.used.use(message.did, wallet.NONCE_PREFIX + message.nonce, message.expires, now)
        issued = issue_token(
            self.key,
            issuer=self.issuer,
            did=message.did,
            audience=resource,
            scopes=scopes,
            ttl_seconds=ttl,
            now=now,
        )
        answer = {
            "ok": True,
            "access_token": issued.access_token,
            "expires_in": issued.expires_in,
            "scope": issued.scope,
        }
        facts = {"kind": "access_token", "did": message.did, "by": "wallet", "token_id": issued.jti}
        return answer, facts


def _active_identity(session: Session, did: str) -> AgentIdentity:
    identity = session.scalar(select(AgentIdentity).where(AgentIdentity.public_identifier == did))
    if identity is None:
        raise TokenRefused("unknown DID-LLM")
    if identity.status != "active":
        raise TokenRefused(f"this DID-LLM is {identity.status}")
    return identity


def _controller(identity: AgentIdentity, controller_type: str) -> str:
    """The DID-LLM's current owner key or wallet of that kind, from the database."""
    if controller_type == grants.ED25519 and identity.controller_public_key:
        return identity.controller_public_key
    if (
        controller_type == grants.EIP155
        and identity.controller_wallet_address
        and identity.controller_network
    ):
        return f"{identity.controller_network}:{identity.controller_wallet_address}"
    raise TokenRefused("this DID-LLM has no such owner key or wallet")
