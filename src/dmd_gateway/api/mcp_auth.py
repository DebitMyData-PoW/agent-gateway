"""The Agentic Superhighway's side of OAuth: the MCP server accepts the access tokens of
DebitMyData's authorization server (api/oauth.py), as an OAuth protected resource (RFC 9728,
MCP authorization).

    GET /.well-known/oauth-protected-resource/agenticsuperhighway   (and .../mcp, its first
                                                                     address)

says which authorization server issues its tokens. Both answer only while agents can sign in.

Signing in is optional. Discovering DebitMyData, quotes, payments and registering a DID-LLM
need no token: the DID-LLM is the login, so a token cannot be needed to get one. A request
that carries a token has it checked in full, every time (signature by the authorization
server's key, issuer, audience = this MCP server, expiry, and the DID-LLM still active), and
is refused with 401 and a ``WWW-Authenticate`` header pointing to the metadata above if any
check fails. Tools that need a signed-in agent (``dmd_whoami``) find its token with
``signed_in``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.auth.middleware.auth_context import auth_context_var, get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from sqlalchemy import select
from starlette.authentication import AuthCredentials
from starlette.types import ASGIApp, Receive, Scope, Send

from dmd_gateway.api import brand
from dmd_gateway.api.get_did_page import mcp_url
from dmd_gateway.api.oauth import PUBLIC, issuer, sign_in_open
from dmd_gateway.db.models import AgentIdentity
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import error_body
from dmd_gateway.oauth.tokens import SCOPES, TokenRejected, verify_token
from dmd_gateway.settings import Settings

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

METADATA_PREFIX = "/.well-known/oauth-protected-resource"
RESOURCE_NAME = "DebitMyData Agentic Superhighway"


def protected_resources(settings: Settings) -> dict[str, str]:
    """Each MCP address (its path) and its resource identifier, the exact URL a token is for."""
    main = main_resource(settings)
    base = settings.public_base_url.rstrip("/")
    found = {urlsplit(main).path: main}
    for path in brand.MCP_OLD_PATHS:
        found.setdefault(path, f"{base}{path}")
    return found


def main_resource(settings: Settings) -> str:
    """The MCP server's resource identifier: its address, as agents connect to it."""
    return mcp_url(settings).rstrip("/")


def metadata_url(resource: str) -> str:
    """Where a resource's metadata is (RFC 9728 section 3.1: the well-known name is inserted
    between the host and the path)."""
    parts = urlsplit(resource)
    return f"{parts.scheme}://{parts.netloc}{METADATA_PREFIX}{parts.path}"


def resource_metadata(settings: Settings, resource: str) -> dict[str, Any]:
    return {
        "resource": resource,
        "authorization_servers": [issuer(settings)],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": RESOURCE_NAME,
        "resource_documentation": f"{settings.public_base_url.rstrip('/')}{brand.LLMS_PATH}",
    }


def _metadata_route(path: str) -> Any:
    def metadata(request: Request) -> Response:
        state = request.app.state
        resource = protected_resources(state.settings).get(path)
        if resource is None or not sign_in_open(state):
            return JSONResponse(status_code=404, content=error_body("not_found", "Not Found"))
        return JSONResponse(resource_metadata(state.settings, resource), headers=PUBLIC)

    return metadata


for _path in (brand.MCP_PATH, *brand.MCP_OLD_PATHS):
    router.add_api_route(
        f"{METADATA_PREFIX}{_path}", _metadata_route(_path), methods=["GET", "HEAD"]
    )


# --------------------------------------------------------------------------- tokens


class Refused(Exception):
    """The request's access token is not accepted: ``description`` is what the agent is told
    (never the issuer or audience this server expects); ``detail`` is only logged."""

    def __init__(self, description: str, detail: str = "") -> None:
        super().__init__(description)
        self.description = description
        self.detail = detail or description


def check_token(state: Any, token: str, now: datetime) -> AccessToken:
    """The signed-in agent, if ``token`` is a valid access token for this MCP server and its
    DID-LLM is active. Runs in a worker thread (it reads the database)."""
    settings: Settings = state.settings
    try:
        claims = verify_token(
            token,
            state.token_signer.public_keys,
            issuer=issuer(settings),
            audiences=list(protected_resources(settings).values()),
            now=now,
        )
    except TokenRejected as exc:
        if exc.expired:
            raise Refused("Token has expired: sign in again") from exc
        raise Refused("The access token is not valid for this server", exc.reason) from exc
    did = claims["sub"]
    with transaction(state.session_factory) as session:
        identity = session.scalar(
            select(AgentIdentity).where(AgentIdentity.public_identifier == did)
        )
        if identity is None:
            raise Refused("The access token is not valid for this server", f"unknown DID {did}")
        if identity.status != "active":
            raise Refused(f"This DID-LLM is {identity.status}")
        identity_id = identity.id
    return AccessToken(
        token=token,
        client_id=str(claims.get("client_id") or did),
        scopes=claims["scope"].split(),
        expires_at=claims["exp"],
        resource=str(claims["aud"]),
        subject=did,
        claims={"iss": claims["iss"], "jti": claims["jti"], "identity_id": identity_id},
    )


def signed_in() -> AccessToken | None:
    """The access token of the request being handled, if it carried a valid one."""
    return get_access_token()


def _authorization(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            return value.decode("latin-1")
    return None


async def _refuse(send: Send, resource: str, description: str) -> None:
    body = json.dumps({"error": "invalid_token", "error_description": description}).encode()
    challenge = (
        f'Bearer error="invalid_token", error_description="{description}", '
        f'resource_metadata="{metadata_url(resource)}"'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", challenge.encode("latin-1")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerTokens:
    """The MCP endpoint, for anyone; with the agent signed in when the request carries a valid
    access token (``Authorization: Bearer ...``), refused with 401 when it carries an invalid
    one. While signing in is not open, tokens are ignored."""

    def __init__(self, endpoint: ASGIApp, settings: Settings) -> None:
        self.endpoint = endpoint
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        header = _authorization(scope) if scope["type"] == "http" else None
        state = scope["app"].state if header is not None else None
        if header is None or not sign_in_open(state):
            await self.endpoint(scope, receive, send)
            return
        resources = protected_resources(self.settings)
        resource = resources.get(scope["path"], main_resource(self.settings))
        kind, _, token = header.partition(" ")
        token = token.strip()
        if kind.lower() != "bearer" or not token:
            await _refuse(send, resource, "Send the access token as: Authorization: Bearer <token>")
            return
        try:
            access = await anyio.to_thread.run_sync(check_token, state, token, datetime.now(UTC))
        except Refused as refused:
            log.info("mcp access token refused: %s", refused.detail)
            await _refuse(send, resource, refused.description)
            return
        user = AuthenticatedUser(access)
        signed = {**scope, "user": user, "auth": AuthCredentials(access.scopes)}
        reset = auth_context_var.set(user)
        try:
            await self.endpoint(signed, receive, send)
        finally:
            auth_context_var.reset(reset)
