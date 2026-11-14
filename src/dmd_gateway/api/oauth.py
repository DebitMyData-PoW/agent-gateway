"""The authorization server, at auth.debitmydata.io (the ``auth_host`` setting): OAuth 2.1.
Phase 1: AI agents get access tokens by signing with their DID-LLM's own key. Phase 2: people
sign in to apps (MCP clients) with their DID-LLM in the browser (api/oauth_browser.py).

    GET  /.well-known/oauth-authorization-server   what this server offers (RFC 8414)
    GET  /.well-known/jwks.json                    the key that signs the access tokens
    POST /token                                    an access token: the client-credentials
                                                   grant, authenticated with a JWT signed by
                                                   the DID-LLM's key (oauth/assertion.py);
                                                   the wallet-signature grant, for DID-LLMs
                                                   owned by a wallet (oauth/wallet_sign_in.py);
                                                   and for apps (phase 2) the authorization
                                                   code (PKCE) and refresh token grants
    POST /challenge                                the message a wallet signs to sign in

Tokens are signed by this process's key (development, staging) or, in production, by the
signer service, which checks every request itself first (signer/tokens.py), including the
signature of each person signing in to an app.

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
from typing import Any, ClassVar
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
from dmd_gateway.errors import ApiError, error_body
from dmd_gateway.oauth import grants
from dmd_gateway.oauth import wallet_sign_in as wallet
from dmd_gateway.oauth.assertion import (
    ASSERTION_TYPE,
    Assertion,
    AssertionRejected,
    database_lookup,
    verify_assertion,
)
from dmd_gateway.oauth.clients import Client, ClientRejected, verify_client_assertion
from dmd_gateway.oauth.grants import Grant, GrantRefused
from dmd_gateway.oauth.keys import jwk_set
from dmd_gateway.oauth.tokens import (
    DEFAULT_SCOPES,
    SCOPES,
    TOKEN_PATH,
    IssuedToken,
    issue_token,
)
from dmd_gateway.services import oauth_sign_in as browser
from dmd_gateway.services.controller_proofs import EIP155, check_signature
from dmd_gateway.services.directory import controllers
from dmd_gateway.settings import Settings
from dmd_gateway.signer.client import SignerRefused, SignerUnavailable

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

METADATA_PATH = "/.well-known/oauth-authorization-server"
JWKS_PATH = "/.well-known/jwks.json"
AUTHORIZE_PATH = "/authorize"
REVOKE_PATH = "/revoke"
AUTH_PATHS = frozenset(
    {METADATA_PATH, JWKS_PATH, TOKEN_PATH, AUTHORIZE_PATH, REVOKE_PATH, wallet.CHALLENGE_PATH}
)
# What the sign-in page needs at the auth host besides its own addresses.
AUTH_ASSETS = frozenset({"/favicon.ico", "/favicon.png", "/logo.png", "/logo-dark.png"})
AUTH_PREFIXES = (AUTHORIZE_PATH + "/", "/fonts/")
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
    """Signs access tokens with a key held by this process (development and staging). The API
    checked the request itself, so this checks only what the signer service would check on top
    (the message's and grant's terms); smart wallets, which the API checks on their chain, can
    sign in."""

    smart_wallets: ClassVar[bool] = True

    key: RSAPrivateKey

    @property
    def public_keys(self) -> tuple[RSAPublicKey, ...]:
        return (self.key.public_key(),)

    def sign_in(
        self,
        *,
        message: str,
        signature: str,
        did: str,
        controller_type: str,
        client_id: str,
        scope: str,
        resource: str,
        issuer: str,
        now: datetime,
    ) -> Grant:
        try:
            checked = grants.check_sign_in(
                message,
                did=did,
                client_id=client_id,
                scope=scope,
                resource=resource,
                issuer=issuer,
                resources=(resource,),
                now=now,
            )
        except GrantRefused as exc:
            raise SignerRefused(f"refused: {exc}") from exc
        return grants.issue_grant(
            self.key,
            issuer=issuer,
            did=did,
            client_id=client_id,
            scope=scope,
            resource=resource,
            nonce=checked.nonce,
            now=now,
        )

    def issue(
        self,
        assertion: str,
        *,
        grant: str | None = None,
        wallet: tuple[str, str] | None = None,
        **claims: Any,
    ) -> IssuedToken:
        # The API checked the agent's signed request or wallet signature, or the app's code or
        # refresh token, itself; this key is its own. Under a grant, its terms still hold.
        if grant is not None:
            try:
                granted = grants.verify_grant(
                    grant, self.public_keys, issuer=claims["issuer"], now=claims["now"]
                )
                grants.check_use(
                    granted,
                    client_id=claims["client_id"],
                    resource=claims["audience"],
                    scopes=claims["scopes"],
                )
            except GrantRefused as exc:
                raise SignerRefused(f"refused: {exc}") from exc
        return issue_token(self.key, **claims)


def issuer(settings: Settings) -> str:
    return f"https://{settings.auth_host}"


def resources(settings: Settings) -> list[str]:
    """The MCP server's addresses a token may be for (RFC 8707 resource indicators)."""
    base = settings.public_base_url.rstrip("/")
    addresses = [mcp_url(settings).rstrip("/"), *(f"{base}{path}" for path in brand.MCP_OLD_PATHS)]
    return list(dict.fromkeys(addresses))


def metadata(settings: Settings, *, browser: bool = False) -> dict[str, Any]:
    iss = issuer(settings)
    document: dict[str, Any] = {
        "issuer": iss,
        "token_endpoint": f"{iss}{TOKEN_PATH}",
        "jwks_uri": f"{iss}{JWKS_PATH}",
        "grant_types_supported": ["client_credentials", wallet.GRANT_TYPE],
        "token_endpoint_auth_methods_supported": ["private_key_jwt", "none"],
        "token_endpoint_auth_signing_alg_values_supported": ["EdDSA"],
        # Agents owned by a wallet: the message to sign, then the wallet-signature grant.
        "dmd_wallet_challenge_endpoint": f"{iss}{wallet.CHALLENGE_PATH}",
        "scopes_supported": list(SCOPES),
        "response_types_supported": [],
        "service_documentation": f"{settings.public_base_url.rstrip('/')}{brand.LLMS_PATH}",
    }
    if browser:
        # Phase 2: people sign in to apps with their DID-LLM (authorization code, PKCE), the
        # apps known by their Client ID Metadata Document (no registration step).
        document.update(
            {
                "authorization_endpoint": f"{iss}{AUTHORIZE_PATH}",
                "revocation_endpoint": f"{iss}{REVOKE_PATH}",
                "grant_types_supported": [
                    "client_credentials",
                    wallet.GRANT_TYPE,
                    "authorization_code",
                    "refresh_token",
                ],
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["private_key_jwt", "none"],
                "token_endpoint_auth_signing_alg_values_supported": [
                    "EdDSA",
                    "RS256",
                    "PS256",
                    "ES256",
                ],
                "revocation_endpoint_auth_methods_supported": ["none"],
                "client_id_metadata_document_supported": True,
                "authorization_response_iss_parameter_supported": True,
            }
        )
    return document


def sign_in_open(state: Any) -> bool:
    """Whether agents can sign in: the auth host is set and a key signs access tokens."""
    return bool(state.settings.auth_host) and state.token_signer is not None


def browser_sign_in_open(state: Any) -> bool:
    """People can sign in to apps in the browser (phase 2): with agents' sign-in. In
    production the signer service checks each person's signature before it gives a grant."""
    return sign_in_open(state)


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
            path = scope["path"]
            allowed = path in AUTH_PATHS or path in AUTH_ASSETS or path.startswith(AUTH_PREFIXES)
            if host == self.auth_host and not allowed:
                await _not_found()(scope, receive, send)
                return
        await self.app(scope, receive, send)


@router.get(METADATA_PATH)
def authorization_server_metadata(request: Request) -> Response:
    if not _enabled(request):
        return _not_found()
    state = request.app.state
    return JSONResponse(
        metadata(state.settings, browser=browser_sign_in_open(state)), headers=PUBLIC
    )


@router.get(JWKS_PATH)
def jwks(request: Request) -> Response:
    if not _enabled(request):
        return _not_found()
    return JSONResponse(jwk_set(*request.app.state.token_signer.public_keys), headers=PUBLIC)


async def read_form(request: Request) -> dict[str, str] | str:
    """The token request's fields, or what is wrong with it. Each field may be sent once
    (RFC 6749 section 3.2), and the whole form is small: a signed request is under 1 KB."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != FORM:
        return f"send the form as {FORM}"
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
    fields = await read_form(request)
    if isinstance(fields, str):
        return _oauth_error(400, "invalid_request", fields)
    grant = fields.get("grant_type")
    if grant in ("authorization_code", "refresh_token") and browser_sign_in_open(request.app.state):
        return await _app_grant(request.app.state, grant, fields)
    if grant == wallet.GRANT_TYPE:
        return await anyio.to_thread.run_sync(_wallet_tokens, request.app.state, fields)
    if grant != "client_credentials":
        return _oauth_error(400, "unsupported_grant_type", "unsupported grant_type")
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


# --------------------------------------------------------------------------- wallets


class _SignerDown(Exception):
    """The signer could not sign: nothing the request did is kept (the code, refresh token or
    signed message stays usable), and the client is told to try again."""


def _wallet_identity(session: Any, did: str) -> tuple[AgentIdentity, str] | str:
    """The active DID-LLM and its owner wallet, or why it cannot sign in with a wallet."""
    identity = session.scalar(select(AgentIdentity).where(AgentIdentity.public_identifier == did))
    if identity is None:
        return "unknown DID-LLM"
    if identity.status != "active":
        return f"this DID-LLM is {identity.status}"
    owner = controllers(identity).get(EIP155)
    if owner is None:
        return "this DID-LLM has no owner wallet: sign in with its key (client_credentials)"
    return identity, owner


@router.post(wallet.CHALLENGE_PATH)
async def wallet_challenge(request: Request) -> Response:
    """The message an agent signs with its DID-LLM's wallet to sign in (5 minutes)."""
    if not _enabled(request):
        return _not_found()
    state = request.app.state
    settings: Settings = state.settings
    fields = await read_form(request)
    if isinstance(fields, str):
        return _oauth_error(400, "invalid_request", fields)
    did = fields.get("did", "").strip()
    if not did:
        return _oauth_error(400, "invalid_request", "send did: your DID-LLM")
    targets = resources(settings)
    resource = fields.get("resource", "").rstrip("/") or targets[0]
    if resource not in targets:
        return _oauth_error(
            400, "invalid_target", f"resource must be the MCP server: {', '.join(targets)}"
        )
    scope = " ".join(fields["scope"].split()) if fields.get("scope", "").strip() else "did:read"
    if any(name not in SCOPES for name in scope.split()):
        return _oauth_error(400, "invalid_scope", f"unknown scope: {scope}")

    def found() -> tuple[AgentIdentity, str] | str:
        with transaction(state.session_factory) as session:
            return _wallet_identity(session, did)

    owner = await anyio.to_thread.run_sync(found)
    if isinstance(owner, str):
        return _oauth_error(400, "invalid_request", owner)
    now = datetime.now(UTC)
    text = wallet.challenge(
        did=did, scope=scope, resource=resource, issuer=issuer(settings), now=now
    )
    return JSONResponse(
        {
            "message": text,
            "wallet": owner[1],
            "expires_in": wallet.MESSAGE_SECONDS,
            "grant_type": wallet.GRANT_TYPE,
            "token_endpoint": f"{issuer(settings)}{TOKEN_PATH}",
        },
        headers=NO_STORE,
    )


def _wallet_tokens(state: Any, fields: dict[str, str]) -> Response:
    try:
        return _wallet_tokens_once(state, fields)
    except _SignerDown as exc:
        log.error("access token for a wallet not signed: %s", exc)
        return _oauth_error(
            503,
            "temporarily_unavailable",
            "signing in is unavailable for a moment: try again in a minute with the same "
            "signed message",
            retry_after=60,
        )


def _wallet_tokens_once(state: Any, fields: dict[str, str]) -> Response:
    """The wallet-signature grant: the message from /challenge, signed by the DID-LLM's owner
    wallet, for an access token. Runs in a worker thread."""
    settings: Settings = state.settings
    now = datetime.now(UTC)
    iss = issuer(settings)
    text, signature = fields.get("message", ""), fields.get("signature", "")
    if not text or not signature:
        return _oauth_error(
            400, "invalid_request", "send message (from /challenge) and signature (its wallet's)"
        )
    try:
        message, scope = wallet.check(
            text,
            issuer=iss,
            resources=resources(settings),
            now=now,
            scope=fields.get("scope") or None,
            resource=fields.get("resource", "").rstrip("/") or None,
        )
    except GrantRefused as exc:
        return _oauth_error(400, "invalid_grant", str(exc))
    if fields.get("client_id") and fields["client_id"] != message.did:
        return _oauth_error(401, "invalid_client", "client_id must be the DID-LLM that signs")
    with transaction(state.session_factory) as session:
        owner = _wallet_identity(session, message.did)
        if isinstance(owner, str):
            return _oauth_error(400, "invalid_grant", owner)
        identity, address = owner
        try:
            checked = check_signature(
                EIP155,
                address,
                text,
                signature,
                still_controller=True,
                wallets=state.wallet_verifier,
            )
        except ApiError as exc:
            if exc.code == "invalid_signature_encoding":
                return _oauth_error(400, "invalid_request", exc.message)
            if exc.code == "smart_wallet_not_supported":
                return _oauth_error(400, "invalid_grant", "the signature is not the owner wallet's")
            return _oauth_error(503, "temporarily_unavailable", exc.message, retry_after=30)
        if not checked.verified:
            return _oauth_error(400, "invalid_grant", "the signature is not the owner wallet's")
        if checked.method not in browser.OFFLINE_METHODS and not state.token_signer.smart_wallets:
            return _oauth_error(
                400,
                "invalid_grant",
                "this is a smart wallet, which cannot sign in here yet: an ordinary wallet can",
            )
        session.execute(
            delete(OAuthAssertion).where(OAuthAssertion.expires_at < now - FORGET_AFTER)
        )
        try:
            with session.begin_nested():
                session.add(
                    OAuthAssertion(
                        did=message.did,
                        jti=wallet.NONCE_PREFIX + message.nonce,
                        expires_at=message.expires,
                    )
                )
        except IntegrityError:
            return _oauth_error(
                400, "invalid_grant", "this message was already used: ask /challenge again"
            )
        try:
            issued: IssuedToken = state.token_signer.issue(
                "",
                issuer=iss,
                did=message.did,
                audience=message.resource,
                scopes=tuple(scope.split()),
                ttl_seconds=settings.oauth_token_ttl_seconds,
                now=now,
                wallet=(text, signature),
            )
        except SignerRefused as exc:
            log.warning("access token for a wallet refused by the signer: %s", exc)
            return _oauth_error(400, "invalid_grant", "the signed message was refused")
        except SignerUnavailable as exc:
            raise _SignerDown(str(exc)) from exc
        audit.record_audit_event(
            session,
            action="oauth.token_issued",
            subject_type="identity",
            subject_id=identity.id,
            actor_type="agent",
            actor_id=message.did,
            data={
                "grant": "wallet-signature",
                "scope": issued.scope,
                "resource": message.resource,
                "token_id": issued.jti,
            },
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


# --------------------------------------------------------------------------- apps (phase 2)


async def _app_grant(state: Any, grant: str, fields: dict[str, str]) -> Response:
    """The authorization code and refresh token grants, for an app a person signed in to."""
    client_id = fields.get("client_id", "")
    if not client_id:
        return _oauth_error(400, "invalid_request", "send client_id")
    try:
        client: Client = await anyio.to_thread.run_sync(state.oauth_clients.get, client_id)
        assertion = fields.get("client_assertion")
        keys = None
        if assertion:
            if fields.get("client_assertion_type") != ASSERTION_TYPE:
                return _oauth_error(400, "invalid_request", "unsupported client_assertion_type")
            if "private_key_jwt" not in client.auth_methods:
                return _oauth_error(401, "invalid_client", "this app does not sign its requests")
            keys = await anyio.to_thread.run_sync(state.oauth_clients.keys, client)
        elif "none" not in client.auth_methods:
            return _oauth_error(401, "invalid_client", "this app must sign its token requests")
    except ClientRejected as exc:
        return _oauth_error(401, "invalid_client", exc.reason)
    return await anyio.to_thread.run_sync(_app_tokens, state, grant, fields, client, keys)


def _app_tokens(
    state: Any,
    grant: str,
    fields: dict[str, str],
    client: Client,
    keys: dict[str, Any] | None,
) -> Response:
    try:
        return _app_tokens_once(state, grant, fields, client, keys)
    except _SignerDown as exc:
        log.error("access token for an app not signed: %s", exc)
        return _oauth_error(
            503,
            "temporarily_unavailable",
            "signing in is unavailable for a moment: try again in a minute",
            retry_after=60,
        )


def _app_tokens_once(
    state: Any,
    grant: str,
    fields: dict[str, str],
    client: Client,
    keys: dict[str, Any] | None,
) -> Response:
    settings: Settings = state.settings
    now = datetime.now(UTC)
    iss = issuer(settings)
    with transaction(state.session_factory) as session:
        if keys is not None:
            try:
                checked = verify_client_assertion(
                    fields["client_assertion"],
                    client,
                    keys,
                    audience=[f"{iss}{TOKEN_PATH}", iss],
                    now=now,
                )
            except ClientRejected as exc:
                return _oauth_error(401, "invalid_client", exc.reason)
            try:
                with session.begin_nested():
                    session.add(
                        OAuthAssertion(
                            did=client.client_id, jti=checked.jti, expires_at=checked.expires_at
                        )
                    )
            except IntegrityError:
                return _oauth_error(401, "invalid_client", "this client_assertion was already used")
        try:
            if grant == "authorization_code":
                code = browser.exchange(
                    session,
                    code=fields.get("code", ""),
                    client_id=client.client_id,
                    redirect_uri=fields.get("redirect_uri", ""),
                    code_verifier=fields.get("code_verifier", ""),
                    resource=fields.get("resource"),
                    now=now,
                )
                assert code.family_id is not None
                family, identity_id, did = code.family_id, code.identity_id, code.did
                scope, resource, granted = code.scope, code.resource, code.sign_in_grant
            else:
                used, scope = browser.use_refresh_token(
                    session,
                    token=fields.get("refresh_token", ""),
                    client_id=client.client_id,
                    scope=fields.get("scope"),
                    resource=fields.get("resource"),
                    now=now,
                )
                family, identity_id, did = used.family_id, used.identity_id, used.did
                resource, granted = used.resource, used.sign_in_grant
        except browser.TokenError as exc:
            # Returning commits what a refused request changed (a revoked family).
            return _oauth_error(exc.status, exc.error, exc.description)
        if granted is None:  # a sign-in from before grants (staging, 0.38.0)
            return _oauth_error(400, "invalid_grant", "this sign-in has ended: sign in again")
        try:
            sign_in_grant = grants.verify_grant(
                granted, state.token_signer.public_keys, issuer=iss, now=now
            )
        except GrantRefused as exc:
            return _oauth_error(400, "invalid_grant", f"{exc}: sign in again")
        try:
            issued: IssuedToken = state.token_signer.issue(
                "",
                issuer=iss,
                did=did,
                audience=resource,
                scopes=tuple(scope.split()),
                ttl_seconds=settings.oauth_token_ttl_seconds,
                now=now,
                client_id=client.client_id,
                grant=granted,
            )
        except SignerRefused as exc:
            log.warning("access token for an app refused by the signer: %s", exc)
            return _oauth_error(400, "invalid_grant", "the sign-in was refused: sign in again")
        except SignerUnavailable as exc:
            raise _SignerDown(str(exc)) from exc
        refresh = browser.new_refresh_token(
            session,
            family_id=family,
            client_id=client.client_id,
            identity_id=identity_id,
            did=did,
            scope=scope,
            resource=resource,
            grant=sign_in_grant,
            now=now,
        )
        audit.record_audit_event(
            session,
            action="oauth.token_issued",
            subject_type="identity",
            subject_id=identity_id,
            actor_type="agent",
            actor_id=did,
            data={
                "grant": grant,
                "client_id": client.client_id,
                "scope": issued.scope,
                "resource": resource,
                "token_id": issued.jti,
            },
        )
    return JSONResponse(
        {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "scope": issued.scope,
            "refresh_token": refresh,
        },
        headers=NO_STORE,
    )
