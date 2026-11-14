"""The authorization server, at auth.debitmydata.io (the ``auth_host`` setting): OAuth 2.1
phase 1, where AI agents get access tokens by signing with their DID-LLM's own key.

    GET  /.well-known/oauth-authorization-server   what this server offers (RFC 8414)
    GET  /.well-known/jwks.json                    the key that signs the access tokens
    POST /token                                    an access token: the client-credentials
                                                   grant, authenticated with a JWT signed by
                                                   the DID-LLM's key (oauth/assertion.py)

They answer at the auth host only, and only while a token-signing key is set. Every other
address at the auth host answers 404 (``AuthHostOnly``), so the authorization server shows
nothing else, and the API's addresses show none of this.

Errors follow RFC 6749 section 5.2 (``{"error": ..., "error_description": ...}``), as OAuth
clients expect, and no answer here is ever cached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

import anyio
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from starlette.types import ASGIApp, Receive, Scope, Send

from dmd_gateway import audit
from dmd_gateway.api import brand
from dmd_gateway.api.get_did_page import mcp_url
from dmd_gateway.db.models import AgentIdentity, OAuthAssertion
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import error_body
from dmd_gateway.oauth.assertion import (
    ASSERTION_TYPE,
    Assertion,
    AssertionRejected,
    database_lookup,
    verify_assertion,
)
from dmd_gateway.oauth.keys import jwk_set
from dmd_gateway.oauth.tokens import (
    DEFAULT_SCOPES,
    SCOPES,
    TOKEN_PATH,
    IssuedToken,
    issue_token,
)
from dmd_gateway.settings import Settings
from dmd_gateway.signer.client import SignerRefused, SignerUnavailable

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

METADATA_PATH = "/.well-known/oauth-authorization-server"
JWKS_PATH = "/.well-known/jwks.json"
AUTH_PATHS = frozenset({METADATA_PATH, JWKS_PATH, TOKEN_PATH})
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PUBLIC = {
    "Cache-Control": "public, max-age=300",
    "Access-Control-Allow-Origin": "*",
    "X-Content-Type-Options": "nosniff",
}
# Used signed requests are forgotten this long after they expire.
FORGET_AFTER = timedelta(hours=1)
FORM = "application/x-www-form-urlencoded"
MAX_FORM_BYTES = 8 * 1024
MAX_FORM_FIELDS = 16


@dataclass(frozen=True)
class LocalTokenSigner:
    """Signs access tokens with a key held by this process (development and staging)."""

    key: RSAPrivateKey

    @property
    def public_keys(self) -> tuple[RSAPublicKey, ...]:
        return (self.key.public_key(),)

    def issue(self, assertion: str, **claims: Any) -> IssuedToken:
        # The API checked the agent's signed request itself; this key is its own.
        return issue_token(self.key, **claims)


def issuer(settings: Settings) -> str:
    return f"https://{settings.auth_host}"


def resources(settings: Settings) -> list[str]:
    """The MCP server's addresses a token may be for (RFC 8707 resource indicators)."""
    base = settings.public_base_url.rstrip("/")
    addresses = [mcp_url(settings).rstrip("/"), *(f"{base}{path}" for path in brand.MCP_OLD_PATHS)]
    return list(dict.fromkeys(addresses))


def metadata(settings: Settings) -> dict[str, Any]:
    iss = issuer(settings)
    return {
        "issuer": iss,
        "token_endpoint": f"{iss}{TOKEN_PATH}",
        "jwks_uri": f"{iss}{JWKS_PATH}",
        "grant_types_supported": ["client_credentials"],
        "token_endpoint_auth_methods_supported": ["private_key_jwt"],
        "token_endpoint_auth_signing_alg_values_supported": ["EdDSA"],
        "scopes_supported": list(SCOPES),
        # Phase 1 has no browser sign-in (authorization endpoint) yet: phase 2.
        "response_types_supported": [],
        "service_documentation": f"{settings.public_base_url.rstrip('/')}{brand.LLMS_PATH}",
    }


def sign_in_open(state: Any) -> bool:
    """Whether agents can sign in: the auth host is set and a key signs access tokens."""
    return bool(state.settings.auth_host) and state.token_signer is not None


def _on_auth_host(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    host = request.headers.get("host", "").strip().lower()
    return bool(settings.auth_host) and host == settings.auth_host


def _enabled(request: Request) -> bool:
    return _on_auth_host(request) and sign_in_open(request.app.state)


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=error_body("not_found", "Not Found"))


def _oauth_error(
    status: int, error: str, description: str, *, retry_after: int | None = None
) -> JSONResponse:
    headers = {**NO_STORE, **({"Retry-After": str(retry_after)} if retry_after else {})}
    return JSONResponse(
        status_code=status,
        content={"error": error, "error_description": description},
        headers=headers,
    )


class AuthHostOnly:
    """At the auth host, only the authorization server's addresses answer."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.auth_host = settings.auth_host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.auth_host and scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1").strip().lower()
            if host == self.auth_host and scope["path"] not in AUTH_PATHS:
                await _not_found()(scope, receive, send)
                return
        await self.app(scope, receive, send)


@router.get(METADATA_PATH)
def authorization_server_metadata(request: Request) -> Response:
    if not _enabled(request):
        return _not_found()
    return JSONResponse(metadata(request.app.state.settings), headers=PUBLIC)


@router.get(JWKS_PATH)
def jwks(request: Request) -> Response:
    if not _enabled(request):
        return _not_found()
    return JSONResponse(jwk_set(*request.app.state.token_signer.public_keys), headers=PUBLIC)


async def _read_form(request: Request) -> dict[str, str] | str:
    """The token request's fields, or what is wrong with it. Each field may be sent once
    (RFC 6749 section 3.2), and the whole form is small: a signed request is under 1 KB."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return f"the form is larger than {MAX_FORM_BYTES} bytes"
    try:
        pairs = parse_qsl(
            body.decode("ascii"), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS
        )
    except (UnicodeDecodeError, ValueError):
        return "the form could not be read"
    fields: dict[str, str] = {}
    for name, value in pairs:
        if name in fields:
            return f"{name} is sent more than once"
        fields[name] = value
    return fields


@router.post(TOKEN_PATH)
async def token(request: Request) -> Response:
    if not _enabled(request):
        return _not_found()
    settings: Settings = request.app.state.settings
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != FORM:
        return _oauth_error(400, "invalid_request", f"send the form as {FORM}")
    fields = await _read_form(request)
    if isinstance(fields, str):
        return _oauth_error(400, "invalid_request", fields)
    if fields.get("grant_type") != "client_credentials":
        return _oauth_error(
            400, "unsupported_grant_type", "this server issues tokens for client_credentials only"
        )
    assertion = fields.get("client_assertion", "")
    if fields.get("client_assertion_type") != ASSERTION_TYPE or not assertion:
        return _oauth_error(
            400,
            "invalid_request",
            f"authenticate with client_assertion_type={ASSERTION_TYPE} and a client_assertion "
            "signed with your DID-LLM's key",
        )
    targets = resources(settings)
    resource = fields.get("resource", "").rstrip("/") or targets[0]
    if resource not in targets:
        return _oauth_error(
            400, "invalid_target", f"resource must be the MCP server: {', '.join(targets)}"
        )
    requested = (
        tuple(fields["scope"].split()) if fields.get("scope", "").strip() else DEFAULT_SCOPES
    )
    unknown = [name for name in requested if name not in SCOPES]
    if unknown:
        return _oauth_error(400, "invalid_scope", f"unknown scope: {' '.join(unknown)}")
    client_id = fields.get("client_id")
    return await anyio.to_thread.run_sync(
        _issue, request.app.state, assertion, client_id, resource, requested
    )


def _issue(
    state: Any, assertion: str, client_id: str | None, resource: str, requested: tuple[str, ...]
) -> Response:
    """Checks the signed request against the database, then has the token signed (by this
    process's key, or by the signer service in production). Runs in a worker thread."""
    settings: Settings = state.settings
    now = datetime.now(UTC)
    iss = issuer(settings)
    with transaction(state.session_factory) as session:
        try:
            checked: Assertion = verify_assertion(
                assertion,
                audience=[f"{iss}{TOKEN_PATH}", iss],
                controller_key=database_lookup(session),
                now=now,
            )
        except AssertionRejected as exc:
            return _oauth_error(401, "invalid_client", exc.reason)
        if client_id and client_id != checked.did:
            return _oauth_error(401, "invalid_client", "client_id must be the DID-LLM that signs")
        session.execute(
            delete(OAuthAssertion).where(OAuthAssertion.expires_at < now - FORGET_AFTER)
        )
        try:
            with session.begin_nested():
                session.add(
                    OAuthAssertion(did=checked.did, jti=checked.jti, expires_at=checked.expires_at)
                )
        except IntegrityError:
            return _oauth_error(
                401, "invalid_client", "this client_assertion was already used: sign a new one"
            )
        identity = session.scalar(
            select(AgentIdentity).where(AgentIdentity.public_identifier == checked.did)
        )
        try:
            issued: IssuedToken = state.token_signer.issue(
                assertion,
                issuer=iss,
                did=checked.did,
                audience=resource,
                scopes=requested,
                ttl_seconds=settings.oauth_token_ttl_seconds,
                now=now,
            )
        except SignerRefused as exc:
            log.warning("access token refused by the signer: %s", exc)
            return _oauth_error(401, "invalid_client", "the signed request was refused")
        except SignerUnavailable as exc:
            log.error("access token not signed: %s", exc)
            return _oauth_error(
                503,
                "temporarily_unavailable",
                "signing in is unavailable for a moment: sign a new request and try again in "
                "a minute",
                retry_after=60,
            )
        audit.record_audit_event(
            session,
            action="oauth.token_issued",
            subject_type="identity",
            subject_id=identity.id if identity is not None else checked.did,
            actor_type="agent",
            actor_id=checked.did,
            data={"scope": issued.scope, "resource": resource, "token_id": issued.jti},
        )
    return JSONResponse(
        {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "scope": issued.scope,
        },
        headers=NO_STORE,
    )
