"""Access tokens: JWTs (RFC 9068, ``typ: at+jwt``) signed RS256 by the authorization server.

    iss        the authorization server, https://auth.debitmydata.io
    sub        the DID-LLM; client_id is the same DID-LLM (it is its own client)
    aud        the MCP server the token is for (the resource the agent named, RFC 8707)
    scope      what it may do, space-separated
    iat, exp   valid for ``oauth_token_ttl_seconds`` (10 minutes by default)
    jti        random

A token is only accepted by the resource it was issued for (``aud``); the MCP server checks
every claim on every request (0.37.1).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from dmd_gateway.oauth.keys import public_jwk

TOKEN_TYPE = "at+jwt"  # noqa: S105 - the JWT "typ" of access tokens (RFC 9068)
CLOCK_SKEW_SECONDS = 60
# What an access token may allow. Phase 1: reading the DID-LLM's own records. More scopes come
# with the tools that need them (managing a listing, purchases, delegation to Minions).
SCOPES = {"did:read": "read your own DID-LLM's records"}
DEFAULT_SCOPES = ("did:read",)


class TokenRejected(Exception):
    pass


@dataclass(frozen=True)
class IssuedToken:
    access_token: str
    expires_in: int
    scope: str
    jti: str


def issue_token(
    key: RSAPrivateKey,
    *,
    issuer: str,
    did: str,
    audience: str,
    scopes: tuple[str, ...],
    ttl_seconds: int,
    now: datetime,
) -> IssuedToken:
    issued = int(now.timestamp())
    jti = secrets.token_urlsafe(18)
    scope = " ".join(scopes)
    claims = {
        "iss": issuer,
        "sub": did,
        "client_id": did,
        "aud": audience,
        "scope": scope,
        "iat": issued,
        "exp": issued + ttl_seconds,
        "jti": jti,
    }
    headers = {"typ": TOKEN_TYPE, "kid": public_jwk(key.public_key())["kid"]}
    token = jwt.encode(claims, key, algorithm="RS256", headers=headers)
    return IssuedToken(access_token=token, expires_in=ttl_seconds, scope=scope, jti=jti)


def verify_token(
    token: str, key: RSAPublicKey, *, issuer: str, audiences: list[str], now: datetime
) -> dict[str, Any]:
    """The token's claims, if it was signed by this server's key for one of ``audiences`` and
    has not expired; otherwise ``TokenRejected``."""
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or header.get("typ") != TOKEN_TYPE:
            raise TokenRejected("not an access token of this server")
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audiences,
            issuer=issuer,
            options={
                "require": ["iss", "sub", "aud", "exp", "iat", "jti", "scope"],
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise TokenRejected(str(exc)) from exc
    moment = int(now.timestamp())
    if not isinstance(claims["exp"], int) or claims["exp"] < moment - CLOCK_SKEW_SECONDS:
        raise TokenRejected("the token has expired")
    if not isinstance(claims["iat"], int) or claims["iat"] > moment + CLOCK_SKEW_SECONDS:
        raise TokenRejected("the token was issued in the future")
    return claims
