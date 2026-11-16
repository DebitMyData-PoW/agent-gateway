"""Apps that register themselves (OAuth Dynamic Client Registration, RFC 7591), for the apps
that cannot use a Client ID Metadata Document: Google's (Gemini, Search). Google's
connected apps need a ``registration_endpoint`` and a client secret.

    POST /register   an app's metadata (JSON) -> its client_id and client_secret

Only Google's own return addresses can be registered
(``https://oauth-redirect[-sandbox|-test].googleusercontent.com/r/...`` or ``/a/...``), so a
code from a sign-in can only go back to Google. The client_id is an https URL on the host of
the app's first return address: the sign-in message the person signs names that host, and
the token signer checks it, as for every other app (oauth/grants.py). So a person sees, and
signs, that they sign in to an app on Google.

A registered app is confidential: it authenticates at the token and revocation endpoints
with its secret (``client_secret_basic`` or ``client_secret_post``), kept here as its
SHA-256 only. Registration is off unless ``oauth_registration_enabled`` is set, and there are
at most ``MAX_PER_HOUR`` registrations an hour and ``MAX_CLIENTS`` in all; an app that never
signed anyone in is forgotten after ``FORGET_UNUSED_DAYS``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote_plus, urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from dmd_gateway.db.models import OAuthRegisteredClient
from dmd_gateway.oauth.clients import MAX_NAME_LENGTH, Client
from dmd_gateway.oauth.tokens import SCOPES

REGISTER_PATH = "/register"
# Google's return addresses (developers.google.com/connected-apps/mcp/oauth): production,
# sandbox and test; /r/ for account linking, /a/ for App Flip.
GOOGLE_HOSTS = frozenset(
    {
        "oauth-redirect.googleusercontent.com",
        "oauth-redirect-sandbox.googleusercontent.com",
        "oauth-redirect-test.googleusercontent.com",
    }
)
# A path segment: never "." or ".." (a browser would leave Google's /r/ or /a/ path).
_SEGMENT = r"(?!\.\.?(?:/|$))[A-Za-z0-9._~-]+"
GOOGLE_REDIRECT = re.compile(
    r"^https://oauth-redirect(?:-sandbox|-test)?\.googleusercontent\.com"
    rf"/(?:r|a)/{_SEGMENT}(?:/{_SEGMENT})*$"
)
MAX_REDIRECT_URI_LENGTH = 300
MAX_REDIRECT_URIS = 10
MAX_BODY_BYTES = 8 * 1024
AUTH_METHODS = ("client_secret_basic", "client_secret_post")
GRANT_TYPES = ("authorization_code", "refresh_token")
MAX_PER_HOUR = 30
MAX_CLIENTS = 1000
FORGET_UNUSED_DAYS = 7
CLIENT_PATH = "debitmydata-client"
DEFAULT_NAME = "An app on Google"


class RegistrationRefused(Exception):
    """RFC 7591 section 3.2.2: ``error`` is invalid_redirect_uri or invalid_client_metadata
    (or temporarily_unavailable, when there are too many registrations)."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class Registered:
    """A new registration: what the app gets back (the secret, this once only)."""

    row: OAuthRegisteredClient
    secret: str

    def answer(self) -> dict[str, Any]:
        row = self.row
        return {
            "client_id": row.client_id,
            "client_secret": self.secret,
            "client_id_issued_at": int(row.created_at.timestamp()),
            "client_secret_expires_at": 0,
            "client_name": row.client_name,
            "redirect_uris": list(row.redirect_uris),
            "grant_types": list(GRANT_TYPES),
            "response_types": ["code"],
            "token_endpoint_auth_method": row.auth_method,
            "scope": row.scope,
        }


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def secret_matches(client: Client, secret: str | None) -> bool:
    """Whether ``secret`` is the registered app's secret (compared in constant time)."""
    if client.secret_hash is None or not secret:
        return False
    return hmac.compare_digest(secret_hash(secret), client.secret_hash)


def read_metadata(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_BODY_BYTES:
        raise RegistrationRefused("invalid_client_metadata", "the metadata is too large")
    try:
        metadata = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RegistrationRefused("invalid_client_metadata", "send the metadata as JSON") from exc
    if not isinstance(metadata, dict):
        raise RegistrationRefused("invalid_client_metadata", "the metadata is not a JSON object")
    return metadata


def _redirect_uris(metadata: Mapping[str, Any]) -> list[str]:
    uris = metadata.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
        raise RegistrationRefused(
            "invalid_redirect_uri", f"send redirect_uris: 1 to {MAX_REDIRECT_URIS} addresses"
        )
    kept: list[str] = []
    for uri in uris:
        if (
            not isinstance(uri, str)
            or len(uri) > MAX_REDIRECT_URI_LENGTH
            or not GOOGLE_REDIRECT.match(uri)
        ):
            raise RegistrationRefused(
                "invalid_redirect_uri",
                "only Google's return addresses can be registered "
                "(https://oauth-redirect.googleusercontent.com/r/...); other apps use a Client "
                "ID Metadata Document as their client_id",
            )
        if uri not in kept:
            kept.append(uri)
    return kept


def _choice(metadata: Mapping[str, Any], name: str, allowed: tuple[str, ...], default: str) -> str:
    value = metadata.get(name, default)
    if value not in allowed:
        raise RegistrationRefused(
            "invalid_client_metadata", f"{name} must be one of: {', '.join(allowed)}"
        )
    return str(value)


def _subset(metadata: Mapping[str, Any], name: str, allowed: tuple[str, ...]) -> None:
    value = metadata.get(name)
    if value is None:
        return
    if not isinstance(value, list) or any(item not in allowed for item in value):
        raise RegistrationRefused(
            "invalid_client_metadata", f"{name} may only list: {', '.join(allowed)}"
        )


def register(
    session: Session,
    metadata: Mapping[str, Any],
    now: datetime,
    *,
    max_per_hour: int = MAX_PER_HOUR,
    max_clients: int = MAX_CLIENTS,
) -> Registered:
    """Checks an app's metadata and registers it, or ``RegistrationRefused``."""
    uris = _redirect_uris(metadata)
    method = _choice(metadata, "token_endpoint_auth_method", AUTH_METHODS, AUTH_METHODS[0])
    _subset(metadata, "grant_types", GRANT_TYPES)
    _subset(metadata, "response_types", ("code",))
    name = metadata.get("client_name")
    if name is not None and not isinstance(name, str):
        raise RegistrationRefused("invalid_client_metadata", "client_name must be text")
    clean = " ".join((name or "").split())[:MAX_NAME_LENGTH] or DEFAULT_NAME
    scope = metadata.get("scope")
    if scope is None or (isinstance(scope, str) and not scope.strip()):
        scope = " ".join(SCOPES)
    if not isinstance(scope, str) or any(word not in SCOPES for word in scope.split()):
        raise RegistrationRefused(
            "invalid_client_metadata", f"scope may only list: {' '.join(SCOPES)}"
        )

    forget_unused(session, now)
    hour = session.scalar(
        select(func.count())
        .select_from(OAuthRegisteredClient)
        .where(OAuthRegisteredClient.created_at > now - timedelta(hours=1))
    )
    total = session.scalar(select(func.count()).select_from(OAuthRegisteredClient))
    if (hour or 0) >= max_per_hour or (total or 0) >= max_clients:
        raise RegistrationRefused(
            "temporarily_unavailable", "too many registrations: try again later", status=503
        )

    host = urlsplit(uris[0]).hostname or ""
    secret = secrets.token_urlsafe(32)
    row = OAuthRegisteredClient(
        client_id=f"https://{host}/{CLIENT_PATH}/{secrets.token_urlsafe(18)}",
        secret_hash=secret_hash(secret),
        client_name=clean,
        redirect_uris=uris,
        auth_method=method,
        scope=" ".join(dict.fromkeys(scope.split())),
        created_at=now,
    )
    session.add(row)
    return Registered(row, secret)


def forget_unused(session: Session, now: datetime) -> None:
    """Registrations that never signed anyone in, after ``FORGET_UNUSED_DAYS``."""
    session.execute(
        delete(OAuthRegisteredClient).where(
            OAuthRegisteredClient.last_used_at.is_(None),
            OAuthRegisteredClient.created_at < now - timedelta(days=FORGET_UNUSED_DAYS),
        )
    )


def client_of(row: OAuthRegisteredClient) -> Client:
    host = (urlsplit(row.client_id).hostname or "").lower()
    return Client(
        client_id=row.client_id,
        name=row.client_name,
        host=host,
        redirect_uris=tuple(row.redirect_uris),
        auth_methods=frozenset(AUTH_METHODS),
        jwks=None,
        jwks_uri=None,
        secret_hash=row.secret_hash,
    )


def database_lookup(session_factory: Callable[[], Session]) -> Callable[[str], Client | None]:
    """Finds a registered app by its client_id (for ``ClientDirectory``)."""

    def lookup(client_id: str) -> Client | None:
        if not is_registered_id(client_id):
            return None
        with session_factory() as session:
            row = session.get(OAuthRegisteredClient, client_id)
            return client_of(row) if row is not None else None

    return lookup


def is_registered_id(client_id: str) -> bool:
    """Whether the client_id is the kind this server gives at registration."""
    parts = urlsplit(client_id)
    return (
        parts.scheme == "https"
        and (parts.hostname or "") in GOOGLE_HOSTS
        and parts.path.startswith(f"/{CLIENT_PATH}/")
    )


def mark_used(session: Session, client: Client, now: datetime) -> None:
    if client.secret_hash is None:
        return
    row = session.get(OAuthRegisteredClient, client.client_id)
    if row is not None:
        row.last_used_at = now


def basic_credentials(header: str | None) -> tuple[str, str] | None:
    """``Authorization: Basic`` as RFC 6749 section 2.3.1 sends it: the client_id and secret,
    each form-encoded, then joined by a colon and base64-encoded."""
    if not header:
        return None
    kind, _, value = header.partition(" ")
    if kind.lower() != "basic" or not value.strip():
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    client_id, colon, secret = decoded.partition(":")
    if not colon:
        return None
    return unquote_plus(client_id), unquote_plus(secret)
