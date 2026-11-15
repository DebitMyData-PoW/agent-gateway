"""Sign-in grants (OAuth phase 2): what lets the authorization server give an app access
tokens for a DID-LLM, for as long as the person's sign-in lasts.

When the DID-LLM's owner signs the sign-in message (services/oauth_sign_in.py), the token
signer checks the message and returns a **grant**: a JWT signed with the token key, kept by
the API in its database and never given to the app. Each access token for that app, at the
code exchange and at every refresh, is signed against the grant. In production the signer
service makes and checks grants itself (signer/tokens.py): it checks the owner's signature,
so even a compromised API cannot get tokens for a DID-LLM whose owner did not sign in to that
app, nor more than 30 days after the owner signed.

    typ          dmd-grant+jwt (never an access token: another typ, and aud is not the MCP
                 server)
    iss, aud     the authorization server
    sub          the DID-LLM
    client_id    the app (its Client ID Metadata Document's URL)
    scope        what the person allowed
    resource     the MCP server
    nonce        the signed message's nonce
    iat, exp     30 days: then the person signs in again
    jti          random

The sign-in message is built and read here, so the API and the signer read it the same way.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from dmd_gateway.identity.crypto import (
    ControllerKeyError,
    EncodingError,
    decode_signature,
    parse_controller_key,
    xml_datetime,
)
from dmd_gateway.identity.wallets import (
    WalletError,
    decode_wallet_signature,
    recovered_signer,
    wallet_from_account_id,
)
from dmd_gateway.oauth.keys import public_jwk
from dmd_gateway.oauth.tokens import CLOCK_SKEW_SECONDS, SCOPES

GRANT_TYPE = "dmd-grant+jwt"
GRANT_DAYS = 30
SIGN_IN_MINUTES = 15
TITLE = "DebitMyData sign-in"
MAX_NONCE_LENGTH = 64
ED25519 = "ed25519"
EIP155 = "eip155"
_FIELDS = ("Sign in to", "DID-LLM", "Allows", "For", "Sign-in server", "Nonce", "Issued", "Expires")


class GrantRefused(RuntimeError):
    """The sign-in or the grant does not hold up; nothing is signed."""


def scope_words(scope: str) -> str:
    """The scope as the person reads it in the message."""
    return "; ".join(SCOPES.get(name, name) for name in scope.split())


# --------------------------------------------------------------------------- the message


@dataclass(frozen=True)
class SignInMessage:
    app: str  # "Example App (app.example.com)"
    app_host: str
    did: str
    allows: str
    resource: str
    issuer: str
    nonce: str
    issued: datetime
    expires: datetime


def sign_in_message(
    *,
    app: str,
    did: str,
    scope: str,
    resource: str,
    issuer: str,
    nonce: str,
    issued: datetime,
    expires: datetime,
) -> str:
    """The exact text the DID-LLM's owner signs (lines joined by \\n, no trailing newline)."""
    return "\n".join(
        [
            TITLE,
            f"Sign in to: {app}",
            f"DID-LLM: {did}",
            f"Allows: {scope_words(scope)}",
            f"For: {resource}",
            f"Sign-in server: {issuer}",
            f"Nonce: {nonce}",
            f"Issued: {xml_datetime(issued)}",
            f"Expires: {xml_datetime(expires)}",
        ]
    )


def message_time(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GrantRefused("the message's times are not dates") from exc
    if moment.tzinfo is None:
        raise GrantRefused("the message's times need a time zone")
    return moment


def parse_sign_in_message(text: str) -> SignInMessage:
    lines = text.split("\n")
    if len(lines) != len(_FIELDS) + 1 or lines[0] != TITLE:
        raise GrantRefused("not a DebitMyData sign-in message")
    values: dict[str, str] = {}
    for name, line in zip(_FIELDS, lines[1:], strict=True):
        prefix = f"{name}: "
        if not line.startswith(prefix) or not line[len(prefix) :].strip():
            raise GrantRefused(f"the message's line {name!r} is missing")
        values[name] = line[len(prefix) :]
    app = values["Sign in to"]
    name, separator, host = app.rpartition(" (")
    if not separator or not name or not host.endswith(")") or len(host) < 3:
        raise GrantRefused("the message does not name the app's site")
    return SignInMessage(
        app=app,
        app_host=host[:-1].lower(),
        did=values["DID-LLM"],
        allows=values["Allows"],
        resource=values["For"],
        issuer=values["Sign-in server"],
        nonce=values["Nonce"],
        issued=message_time(values["Issued"]),
        expires=message_time(values["Expires"]),
    )


def check_sign_in(
    text: str,
    *,
    did: str,
    client_id: str,
    scope: str,
    resource: str,
    issuer: str,
    resources: Sequence[str],
    now: datetime,
) -> SignInMessage:
    """The signed message, if it is this sign-in server's message for exactly this DID-LLM,
    app (its site), scope and MCP server, and still valid; otherwise ``GrantRefused``."""
    message = parse_sign_in_message(text)
    skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
    if message.issuer != issuer:
        raise GrantRefused("the message is for another sign-in server")
    if resource not in resources or message.resource != resource:
        raise GrantRefused("the message is for another service")
    if message.did != did:
        raise GrantRefused("the message is for another DID-LLM")
    if message.app_host != (urlsplit(client_id).hostname or "").lower():
        raise GrantRefused("the message names another app")
    names = scope.split()
    if (
        not names
        or any(name not in SCOPES for name in names)
        or message.allows != scope_words(scope)
    ):
        raise GrantRefused("the message allows something else")
    if not 0 < len(message.nonce) <= MAX_NONCE_LENGTH:
        raise GrantRefused("the message's nonce is not usable")
    if message.issued > now + skew:
        raise GrantRefused("the message was issued in the future")
    if message.expires < now - skew:
        raise GrantRefused("the sign-in has expired: start again from the app")
    if message.expires - message.issued > timedelta(minutes=SIGN_IN_MINUTES) + skew:
        raise GrantRefused(f"a sign-in lasts {SIGN_IN_MINUTES} minutes at most")
    return message


def owner_signed(controller_type: str, controller: str, message: str, signature: str) -> bool:
    """Whether ``signature`` is the controller's signature of ``message``, checked with no
    network: an Ed25519 key, or an ordinary wallet (EIP-191). A smart wallet's signature cannot
    be checked without asking its chain, so it does not count here."""
    try:
        if controller_type == ED25519:
            key = parse_controller_key(controller)
            return key.verify(decode_signature(signature), message.encode("utf-8"))
        if controller_type == EIP155:
            wallet = wallet_from_account_id(controller)
            return recovered_signer(message, decode_wallet_signature(signature)) == wallet.address
    except (ControllerKeyError, EncodingError, WalletError) as exc:
        raise GrantRefused(f"the signature cannot be read: {exc}") from exc
    raise GrantRefused("unknown kind of owner key or wallet")


# --------------------------------------------------------------------------- the grant


@dataclass(frozen=True)
class Grant:
    token: str
    did: str
    client_id: str
    scope: str
    resource: str
    expires_at: datetime
    jti: str
    issued_at: datetime | None = None


def before_owner_change(issued_at: int | float | datetime, changed_at: datetime | None) -> bool:
    """Whether a sign-in (a token or grant issued at ``issued_at``, in whole seconds) may have
    been made before the DID-LLM's owners last changed: it then no longer counts. One made in
    the very second of the change does not count either (an issue time has no finer
    precision): the new owner signs in again a second later."""
    if changed_at is None:
        return False
    issued = issued_at.timestamp() if isinstance(issued_at, datetime) else float(issued_at)
    return int(issued) <= int(changed_at.timestamp())


def issue_grant(
    key: RSAPrivateKey,
    *,
    issuer: str,
    did: str,
    client_id: str,
    scope: str,
    resource: str,
    nonce: str,
    now: datetime,
) -> Grant:
    issued = int(now.timestamp())
    expires = issued + GRANT_DAYS * 86400
    jti = secrets.token_urlsafe(18)
    claims = {
        "iss": issuer,
        "aud": issuer,
        "sub": did,
        "client_id": client_id,
        "scope": scope,
        "resource": resource,
        "nonce": nonce,
        "iat": issued,
        "exp": expires,
        "jti": jti,
    }
    headers = {"typ": GRANT_TYPE, "kid": public_jwk(key.public_key())["kid"]}
    token = jwt.encode(claims, key, algorithm="RS256", headers=headers)
    return Grant(token, did, client_id, scope, resource, datetime.fromtimestamp(expires, UTC), jti)


def verify_grant(token: str, keys: Sequence[RSAPublicKey], *, issuer: str, now: datetime) -> Grant:
    """The grant, if one of ``keys`` signed it for this sign-in server and it has not expired;
    otherwise ``GrantRefused``."""
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or header.get("typ") != GRANT_TYPE:
            raise GrantRefused("not a sign-in grant of this server")
        kid = header.get("kid")
        key = next((k for k in keys if public_jwk(k)["kid"] == kid), None)
        if key is None:
            raise GrantRefused("the grant is signed by an unknown key")
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=issuer,
            issuer=issuer,
            options={
                "require": ["iss", "aud", "sub", "exp", "iat", "jti"],
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise GrantRefused(f"the grant is not valid: {exc}") from exc
    for name in ("sub", "client_id", "scope", "resource", "jti"):
        if not isinstance(claims.get(name), str) or not claims[name]:
            raise GrantRefused(f"the grant has no {name}")
    if not isinstance(claims["exp"], int) or claims["exp"] < int(now.timestamp()):
        raise GrantRefused("the sign-in has ended (30 days)")
    return Grant(
        token=token,
        did=claims["sub"],
        client_id=claims["client_id"],
        scope=claims["scope"],
        resource=claims["resource"],
        expires_at=datetime.fromtimestamp(claims["exp"], UTC),
        jti=claims["jti"],
        issued_at=datetime.fromtimestamp(claims["iat"], UTC)
        if isinstance(claims.get("iat"), int)
        else None,
    )


def check_use(grant: Grant, *, client_id: str, resource: str, scopes: Sequence[str]) -> None:
    """A token under the grant: for the same app and MCP server, the same scope or less."""
    if grant.client_id != client_id:
        raise GrantRefused("the grant is for another app")
    if grant.resource != resource:
        raise GrantRefused("the grant is for another service")
    allowed = grant.scope.split()
    if not scopes or any(name not in allowed for name in scopes):
        raise GrantRefused("the token would allow more than the person did")
