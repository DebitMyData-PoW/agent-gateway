"""What the signer checks before it signs an OAuth access token (OAuth phase 1, production).

In production the token-signing key is the signer's, like the issuer key: the API asks
(``{"op": "token", ...}``) with the agent's signed request, and the signer checks that request
itself, through its read-only database role, before signing anything:

* the request is a JWT signed with the Ed25519 key that controls an active DID-LLM, for this
  authorization server (``aud``), valid for 5 minutes at most and not expired (the same checks
  as the API's, oauth/assertion.py);
* each signed request is used once: the signer remembers them until they expire (in memory;
  they expire within minutes, so a restart forgets nothing that still matters);
* the token is for this gateway's MCP server only (its address, or its first address /mcp),
  with known scopes, for 1 to 60 minutes.

So even a fully compromised API server cannot have the signer make a token for a DID-LLM that
did not just sign in itself, nor a token for another service.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from sqlalchemy.orm import Session

from dmd_gateway.api.brand import MCP_OLD_PATHS, MCP_PATH
from dmd_gateway.oauth.assertion import (
    CLOCK_SKEW_SECONDS,
    AssertionRejected,
    database_lookup,
    verify_assertion,
)
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

    def issue(
        self, session: Session, request: Mapping[str, Any], now: datetime
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """The signed token and what it was checked against, or ``TokenRefused``."""
        assertion, resource = request.get("assertion"), request.get("resource")
        scope, ttl = request.get("scope"), request.get("ttl_seconds")
        if not isinstance(assertion, str) or not isinstance(resource, str):
            raise TokenRefused("a token request needs an assertion and a resource")
        if not isinstance(scope, str) or not isinstance(ttl, int) or isinstance(ttl, bool):
            raise TokenRefused("a token request needs a scope and ttl_seconds")
        if resource not in self.resources:
            raise TokenRefused("the token would be for another service")
        scopes = tuple(scope.split())
        if not scopes or any(name not in SCOPES for name in scopes):
            raise TokenRefused("unknown scope")
        if not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
            raise TokenRefused("ttl_seconds must be 60 to 3600")
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
