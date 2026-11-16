"""The Agentic Superhighway's side of OAuth: the MCP server accepts the access tokens of
DebitMyData's authorization server (api/oauth.py), as an OAuth protected resource (RFC 9728,
MCP authorization).

    GET /.well-known/oauth-protected-resource/agenticsuperhighway   (and .../mcp, its first
                                                                     address)
    GET /.well-known/oauth-protected-resource/agenticsuperhighway/identity

says which authorization server issues its tokens. They answer only while agents can sign in.

The identity endpoint (``/agenticsuperhighway/identity``) is a separate resource with three
read-only tools, every one of them for a signed-in DID-LLM: every request there needs a token
(401 otherwise, from the first one), a token is issued for one endpoint or the other (its
``aud``), and each endpoint refuses the other's.

Signing in is optional. Discovering DebitMyData, quotes, payments and registering a DID-LLM
need no token: the DID-LLM is the login, so a token cannot be needed to get one. A request
that carries a token has it checked in full, every time (signature by the authorization
server's key, issuer, audience = this MCP server, expiry, and the DID-LLM still active), and
is refused with 401 and a ``WWW-Authenticate`` header pointing to the metadata above if any
check fails. Tools that need a signed-in agent (``debitmydata_whoami``) find its token with
``signed_in``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.auth.middleware.auth_context import auth_context_var, get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from sqlalchemy import exists, select
from starlette.authentication import AuthCredentials
from starlette.types import ASGIApp, Receive, Scope, Send

from dmd_gateway.api import brand
from dmd_gateway.api.get_did_page import mcp_url
from dmd_gateway.api.oauth import PUBLIC, issuer, sign_in_open
from dmd_gateway.db.models import AgentIdentity, OAuthRefreshToken
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import error_body
from dmd_gateway.oauth.grants import before_owner_change
from dmd_gateway.oauth.tokens import SCOPES, TokenRejected, verify_token
from dmd_gateway.settings import Settings

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

METADATA_PREFIX = "/.well-known/oauth-protected-resource"
RESOURCE_NAME = "DebitMyData Agentic Superhighway"
IDENTITY_RESOURCE_NAME = "DebitMyData DID-LLM identity"


def main_resources(settings: Settings) -> dict[str, str]:
    """The Agentic Superhighway's addresses (each path and its resource identifier, the exact
    URL a token is for): its address, and its earlier one."""
    main = main_resource(settings)
    base = settings.public_base_url.rstrip("/")
    found = {urlsplit(main).path: main}
    for path in brand.MCP_OLD_PATHS:
        found.setdefault(path, f"{base}{path}")
    return found


def protected_resources(settings: Settings) -> dict[str, str]:
    """Every MCP address a token may be for: the Agentic Superhighway's and the identity
    endpoint's."""
    identity = identity_resource(settings)
    return {**main_resources(settings), urlsplit(identity).path: identity}


def main_resource(settings: Settings) -> str:
    """The MCP server's resource identifier: its address, as agents connect to it."""
    return mcp_url(settings).rstrip("/")


def identity_resource(settings: Settings) -> str:
    """The identity endpoint's resource identifier: the MCP server's address + /identity."""
    return main_resource(settings) + brand.IDENTITY_MCP_PATH.removeprefix(brand.MCP_PATH)


def metadata_url(resource: str) -> str:
    """Where a resource's metadata is (RFC 9728 section 3.1: the well-known name is inserted
    between the host and the path)."""
    parts = urlsplit(resource)
    return f"{parts.scheme}://{parts.netloc}{METADATA_PREFIX}{parts.path}"


def resource_metadata(settings: Settings, resource: str) -> dict[str, Any]:
    identity = resource == identity_resource(settings)
    return {
        "resource": resource,
        "authorization_servers": [issuer(settings)],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": IDENTITY_RESOURCE_NAME if identity else RESOURCE_NAME,
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


for _path in (brand.MCP_PATH, *brand.MCP_OLD_PATHS, brand.IDENTITY_MCP_PATH):
    router.add_api_route(
        f"{METADATA_PREFIX}{_path}", _metadata_route(_path), methods=["GET", "HEAD"]
    )
# The root form, which some clients try when they find no other: the main MCP address.
router.add_api_route(METADATA_PREFIX, _metadata_route(brand.MCP_PATH), methods=["GET", "HEAD"])


# --------------------------------------------------------------------------- tokens


class Refused(Exception):
    """The request's access token is not accepted: ``description`` is what the agent is told
    (never the issuer or audience this server expects); ``detail`` is only logged."""

    def __init__(self, description: str, detail: str = "") -> None:
        super().__init__(description)
        self.description = description
        self.detail = detail or description


def check_token(
    state: Any, token: str, now: datetime, audiences: Sequence[str] | None = None
) -> AccessToken:
    """The signed-in agent, if ``token`` is a valid access token for this endpoint
    (``audiences``: the Agentic Superhighway's addresses unless given), its DID-LLM is active
    and, for an app, its sign-in has not ended. Runs in a worker thread (it reads the
    database)."""
    settings: Settings = state.settings
    if audiences is None:
        audiences = list(main_resources(settings).values())
    try:
        claims = verify_token(
            token,
            state.token_signer.public_keys,
            issuer=issuer(settings),
            audiences=list(audiences),
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
        if before_owner_change(claims["iat"], identity.controller_changed_at):
            raise Refused(
                "This DID-LLM's owners changed after this sign-in: sign in again",
                f"token issued before the owner change of {did}",
            )
        sid = claims.get("sid")
        if sid is not None and not session.scalar(
            select(
                exists().where(
                    OAuthRefreshToken.family_id == str(sid),
                    OAuthRefreshToken.revoked_at.is_(None),
                )
            )
        ):
            raise Refused(
                "This sign-in was ended: sign in again", f"token of the ended sign-in {sid}"
            )
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


async def _refuse(
    send: Send, resource: str, description: str, scope: str = "", *, missing: bool = False
) -> None:
    """401 with the challenge that starts the sign-in. ``missing``: the request carried no
    token at all, so the challenge names no error (RFC 6750, section 3.1), as MCP clients
    expect when they connect: only where to sign in (resource_metadata) and the scope."""
    body = json.dumps({"error": "invalid_token", "error_description": description}).encode()
    error = "" if missing else f'error="invalid_token", error_description="{description}", '
    challenge = f'Bearer {error}resource_metadata="{metadata_url(resource)}"' + (
        f', scope="{scope}"' if scope else ""
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


# Tools that need a signed-in DID-LLM. Called without a token, they are answered with HTTP
# 401 and a WWW-Authenticate challenge before the MCP server runs: what makes MCP clients such
# as Claude offer the sign-in ("lazy authentication"); a tool result cannot do it.
PROTECTED_TOOLS = frozenset({"debitmydata_whoami"})
MAX_PEEK_BYTES = 1024 * 1024


def _calls_protected_tool(body: bytes) -> str | None:
    """The scope a JSON-RPC request needs, if it calls a protected tool."""
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    for message in parsed if isinstance(parsed, list) else [parsed]:
        if (
            isinstance(message, dict)
            and message.get("method") == "tools/call"
            and isinstance(message.get("params"), dict)
            and message["params"].get("name") in PROTECTED_TOOLS
        ):
            return "did:read"
    return None


async def peek(receive: Receive) -> tuple[bytes, Receive]:
    """The request's body (up to MAX_PEEK_BYTES), and a ``receive`` that gives it again."""
    chunks: list[bytes] = []
    size, more = 0, True
    while more and size <= MAX_PEEK_BYTES:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        chunks.append(chunk)
        size += len(chunk)
        more = bool(message.get("more_body", False))
    body = b"".join(chunks)
    replayed = False

    async def replay() -> Any:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": more}
        return await receive()

    return body, replay


class BearerTokens:
    """The MCP endpoint, for anyone; with the agent signed in when the request carries a valid
    access token (``Authorization: Bearer ...``), refused with 401 when it carries an invalid
    one, or none for a tool that needs it. While signing in is not open, tokens are ignored.

    ``identity``: the identity endpoint, where every request needs a token for that endpoint
    (initialize and tools/list too): the plain MCP authorization flow, where a client that gets
    a 401 when it connects signs in (Grok, Google and other clients that do not act on a tool's
    sign-in hint). CORS preflights (OPTIONS) carry no token and pass."""

    def __init__(self, endpoint: ASGIApp, settings: Settings, *, identity: bool = False) -> None:
        self.endpoint = endpoint
        self.settings = settings
        self.identity = identity

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not sign_in_open(scope["app"].state):
            await self.endpoint(scope, receive, send)
            return
        state = scope["app"].state
        header = _authorization(scope)
        if self.identity:
            resource = identity_resource(self.settings)
            audiences = [resource]
        else:
            resources = main_resources(self.settings)
            resource = resources.get(scope["path"], main_resource(self.settings))
            audiences = list(resources.values())
        if header is None:
            if self.identity and scope.get("method") != "OPTIONS":
                await _refuse(
                    send, resource, "Sign in with your DID-LLM", scope="did:read", missing=True
                )
                return
            if scope.get("method") == "POST":
                body, receive = await peek(receive)
                needed = _calls_protected_tool(body)
                if needed:
                    await _refuse(
                        send, resource, "Sign in with your DID-LLM", scope=needed, missing=True
                    )
                    return
            await self.endpoint(scope, receive, send)
            return
        kind, _, token = header.partition(" ")
        token = token.strip()
        if kind.lower() != "bearer" or not token:
            await _refuse(send, resource, "Send the access token as: Authorization: Bearer <token>")
            return
        try:
            access = await anyio.to_thread.run_sync(
                check_token, state, token, datetime.now(UTC), audiences
            )
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
