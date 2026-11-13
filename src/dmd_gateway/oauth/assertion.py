"""The agent's signed request for a token: a JWT signed with its DID-LLM's own key.

RFC 7523 client authentication (``client_assertion_type`` =
``urn:ietf:params:oauth:client-assertion-type:jwt-bearer``), as MCP's Client Credentials
extension recommends. The agent's DID-LLM is the client, so the JWT says:

    header   {"alg": "EdDSA", "typ": "JWT"}
    claims   iss = sub = the DID-LLM; aud = the token endpoint's address;
             iat and exp at most 5 minutes apart; jti, a random value used once

and is signed with the Ed25519 key that controls the DID-LLM (the same key that proved
control when it was registered). It is accepted only if the DID-LLM is active and that key
still controls it. DID-LLMs controlled by a wallet sign in through the browser (phase 2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from dmd_gateway.identity.crypto import ControllerKeyError, parse_controller_key

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
MAX_LIFETIME_SECONDS = 300
CLOCK_SKEW_SECONDS = 60
MAX_JTI_LENGTH = 128
REQUIRED_CLAIMS = ["iss", "sub", "aud", "exp", "iat", "jti"]


class AssertionRejected(Exception):
    """The signed request is not acceptable; ``reason`` says why, for the agent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Assertion:
    did: str
    jti: str
    expires_at: datetime


# For a DID-LLM: its controller key (``ed25519:z6Mk...``) if it is active and controlled by
# one, the reason it cannot sign in otherwise.
KeyLookup = Callable[[str], "str | AssertionRejected"]


def _number(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionRejected(f"{name} must be a whole number of seconds")
    return value


def verify_assertion(
    token: str, *, audience: str | list[str], controller_key: KeyLookup, now: datetime
) -> Assertion:
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise AssertionRejected("client_assertion is not a JWT") from exc
    if header.get("alg") != "EdDSA":
        raise AssertionRejected("client_assertion must be signed with EdDSA (Ed25519)")
    did = unverified.get("iss")
    if not isinstance(did, str) or not did.startswith("did:"):
        raise AssertionRejected("iss must be the DID-LLM that signs in")
    found = controller_key(did)
    if isinstance(found, AssertionRejected):
        raise found
    try:
        public = Ed25519PublicKey.from_public_bytes(parse_controller_key(found).raw)
    except ControllerKeyError as exc:
        raise AssertionRejected("this DID-LLM's controller key cannot sign in") from exc
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            public,
            algorithms=["EdDSA"],
            audience=audience,
            issuer=did,
            options={
                "require": REQUIRED_CLAIMS,
                "verify_exp": False,  # checked below, against ``now``
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.InvalidSignatureError as exc:
        raise AssertionRejected("the signature is not the DID-LLM controller's") from exc
    except jwt.InvalidAudienceError as exc:
        raise AssertionRejected("aud must be the token endpoint's address") from exc
    except jwt.PyJWTError as exc:
        raise AssertionRejected(f"client_assertion: {exc}") from exc
    if claims["sub"] != did:
        raise AssertionRejected("sub must be the same DID-LLM as iss")
    issued, expires = _number(claims, "iat"), _number(claims, "exp")
    moment = int(now.timestamp())
    if expires - issued > MAX_LIFETIME_SECONDS or expires <= issued:
        raise AssertionRejected("exp must be after iat, by 5 minutes at most")
    if issued > moment + CLOCK_SKEW_SECONDS:
        raise AssertionRejected("iat is in the future")
    if expires < moment - CLOCK_SKEW_SECONDS:
        raise AssertionRejected("client_assertion has expired: sign a new one")
    if "nbf" in claims and _number(claims, "nbf") > moment + CLOCK_SKEW_SECONDS:
        raise AssertionRejected("client_assertion is not valid yet (nbf)")
    jti = claims["jti"]
    if not isinstance(jti, str) or not 1 <= len(jti) <= MAX_JTI_LENGTH:
        raise AssertionRejected(f"jti must be a string of 1 to {MAX_JTI_LENGTH} characters")
    return Assertion(did=did, jti=jti, expires_at=datetime.fromtimestamp(expires, UTC))
