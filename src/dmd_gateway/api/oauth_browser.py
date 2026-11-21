"""Signing in to an app with a DID-LLM, in the browser (OAuth phase 2), at the auth host:

    GET  /authorize                     an app's authorization request (code flow, PKCE S256)
    GET  /authorize/{id}                the sign-in page: the DID-LLM, then one signature
    GET  /authorize/{id}/request        what to sign (the page and the owner-key tool)
    POST /authorize/{id}/did            the DID-LLM to sign in with: the message to sign
    POST /authorize/{id}/signature      the owner's signature of it
    POST /authorize/{id}/finish         back to the app, with the authorization code
    POST /authorize/{id}/deny           back to the app, without signing in
    POST /revoke                        an app ends a sign-in (RFC 7009)

How it works: services/oauth_sign_in.py. The apps are known by their Client ID Metadata
Document (oauth/clients.py): any MCP client can ask, and the page always shows which site
asks. An unknown app or a redirect address it does not list is answered here, never sent
anywhere; every other answer goes back to the app with ``iss`` (RFC 9207). Only where this
process signs the tokens itself (staging); production waits for the signer to check browser
sign-ins too.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from dmd_gateway.api import brand, page_design
from dmd_gateway.api.mcp_auth import identity_resource, main_resource, protected_resources
from dmd_gateway.api.oauth import (
    AUTHORIZE_PATH,
    REVOKE_PATH,
    browser_sign_in_open,
    client_credentials,
    issuer,
    read_form,
)
from dmd_gateway.db.models import AgentIdentity
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import ApiError, error_body
from dmd_gateway.oauth import registration
from dmd_gateway.oauth.clients import LOOPBACK_HOSTS, ClientRejected, redirect_allowed
from dmd_gateway.oauth.tokens import SCOPES, TokenRejected, verify_token
from dmd_gateway.services import oauth_sign_in as flow
from dmd_gateway.services.oauth_sign_in import CHALLENGE, AuthorizationRequest

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

_STATIC = files("dmd_gateway") / "static"
_STYLE = (
    brand.CSS
    + (_STATIC / "get-did.css").read_text(encoding="utf-8")
    + (_STATIC / "sign-in.css").read_text(encoding="utf-8")
)
# The page's style in each design (page_design): its own, then the chosen design's.
STYLES = page_design.styles(_STYLE)
_SCRIPT = (_STATIC / "sign-in.js").read_text(encoding="utf-8")
OWNER_KEY_GUIDE = "https://github.com/DebitMyData-PoW/agent-gateway/blob/main/docs/owner-key.md"


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _csp(style: str) -> str:
    return (
        f"default-src 'none'; script-src 'sha256-{_sha256(_SCRIPT)}' "
        f"{brand.THEME_SCRIPT_SOURCE}; style-src 'sha256-{_sha256(style)}'; font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    )


CSPS = {design: _csp(style) for design, style in STYLES.items()}
CSP = CSPS[page_design.CLASSIC]
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(
    request: Request, title: str, body: str, config: dict[str, Any] | None = None
) -> HTMLResponse:
    design, kept = page_design.chosen(request)
    tail = ""
    if config is not None:
        data = json.dumps(config).replace("<", "\\u003c")
        tail = (
            f'<script type="application/json" id="page-config">{data}</script>'
            f"<script>{_SCRIPT}</script>"
        )
    page = (
        f"<!doctype html><html lang=en{page_design.root_attribute(design)}><head>"
        '<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)} · DebitMyData</title><meta name=robots content=noindex>"
        f'<link rel=icon href="/favicon.ico" sizes=any>{page_design.preload(design)}'
        f"{brand.THEME_SCRIPT}<style>{STYLES[design]}</style></head><body class=sign-in>"
        f"<header class=sign-in-top><span class=brand>{brand.LOGO}<b>DebitMyData</b></span>"
        f"{brand.THEME_BUTTON}</header><main id=main>{body}</main>"
        "<p class=fine>DebitMyData never asks for a private key or a recovery phrase. Signing "
        "is free and sends nothing.</p>"
        f"{tail}</body></html>"
    )
    response = HTMLResponse(page, headers={**HEADERS, "Content-Security-Policy": CSPS[design]})
    page_design.remember(request, response, kept)
    return response


def _problem_page(request: Request, text: str, status: int = 400) -> HTMLResponse:
    response = _page(
        request,
        "Sign-in problem",
        f"<section class=card><h1>This sign-in cannot continue</h1><p>{_e(text)}</p>"
        "<p>Go back to the app and try again. If it keeps happening, the app's address is "
        "probably not set up for DebitMyData sign-in.</p></section>",
    )
    response.status_code = status
    return response


def back_to_app(state: Any, redirect_uri: str, client_state: str | None, **params: str) -> str:
    """The app's redirect address with the answer, ``state`` and ``iss`` (RFC 9207)."""
    query = {**params, **({"state": client_state} if client_state else {})}
    query["iss"] = issuer(state.settings)
    joiner = "&" if urlsplit(redirect_uri).query else "?"
    return f"{redirect_uri}{joiner}{urlencode(query)}"


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=error_body("not_found", "Not Found"))


def _enabled(request: Request) -> bool:
    host = request.headers.get("host", "").strip().lower()
    state = request.app.state
    return host == state.settings.auth_host and browser_sign_in_open(state)


# --------------------------------------------------------------------------- authorize


@router.get(AUTHORIZE_PATH)
async def authorize(request: Request) -> Response:
    if not _enabled(request):
        return _not_found()
    state = request.app.state
    params = request.query_params
    if any(len(params.getlist(name)) > 1 for name in params):
        return _problem_page(request, "A parameter of the request is sent more than once.")
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    try:
        client = await anyio.to_thread.run_sync(state.oauth_clients.get, client_id)
    except ClientRejected as exc:
        return _problem_page(request, f"The app could not be identified: {exc.reason}.")
    if not redirect_allowed(redirect_uri, client.redirect_uris):
        return _problem_page(
            request, "The app's return address is not one its metadata document lists."
        )
    client_state = params.get("state")
    if client_state is not None and len(client_state) > 512:
        return _problem_page(request, "The app's state parameter is too long.")

    def refuse(error: str, description: str) -> RedirectResponse:
        target = back_to_app(
            state, redirect_uri, client_state, error=error, error_description=description
        )
        return RedirectResponse(target, status_code=303)

    if params.get("response_type") != "code":
        return refuse("unsupported_response_type", "only response_type=code is supported")
    if params.get("code_challenge_method") != "S256":
        return refuse("invalid_request", "PKCE with code_challenge_method=S256 is required")
    challenge = params.get("code_challenge", "")
    if not CHALLENGE.fullmatch(challenge):
        return refuse("invalid_request", "code_challenge must be a base64url SHA-256 (43 chars)")
    scopes = tuple(params.get("scope", "").split()) or ("did:read",)
    unknown = [name for name in scopes if name not in SCOPES]
    if unknown:
        return refuse("invalid_scope", f"unknown scope: {' '.join(unknown)}")
    resources = list(protected_resources(state.settings).values())
    default = main_resource(state.settings)
    if client.registered:
        # Google's apps: the identity endpoint only (read-only, nothing to buy or change).
        resources = [identity_resource(state.settings)]
        default = resources[0]
    resource = params.get("resource", "").rstrip("/") or default
    if resource not in resources:
        return refuse("invalid_target", f"resource must be the MCP server: {', '.join(resources)}")
    authorization = AuthorizationRequest(
        client, redirect_uri, client_state, challenge, scopes, resource
    )
    now = datetime.now(UTC)
    with transaction(state.session_factory) as session:
        sign_in = flow.start(session, authorization, now)
        sign_in_id = sign_in.id
    return RedirectResponse(f"{AUTHORIZE_PATH}/{sign_in_id}", status_code=303)


@router.get(AUTHORIZE_PATH + "/{sign_in_id}")
def sign_in_page(request: Request, sign_in_id: str) -> Response:
    if not _enabled(request):
        return _not_found()
    state = request.app.state
    try:
        with transaction(state.session_factory) as session:
            sign_in = flow.find(session, sign_in_id, datetime.now(UTC))
            app, allows, status = (
                sign_in.client_name,
                flow.scope_words(sign_in.scope),
                sign_in.status,
            )
            back = urlsplit(sign_in.redirect_uri).hostname or ""
    except ApiError as exc:
        return _problem_page(request, exc.message, exc.status_code)
    if status not in ("open", "approved"):
        return _problem_page(request, f"This sign-in is {status}: start again from the app.", 409)
    body = (
        "<section class=card>"
        "<h1>Sign in with your DID-LLM</h1>"
        f"<p class=app><b>{_e(app)}</b> asks to sign you in with your DebitMyData DID-LLM.</p>"
        f"<p class=allows>It will be able to: <b>{_e(allows)}</b>. Nothing else: no payment, "
        "no change to your DID-LLM.</p>"
        f"<p class=small>After signing in you go back to <b>{_e(back)}</b>."
        + (
            " That is an app on this computer: continue only if you started this sign-in "
            "from it yourself."
            if back in LOOPBACK_HOSTS
            else ""
        )
        + "</p>"
        "<form id=find novalidate><label for=did>Your DID-LLM</label>"
        "<input id=did name=did type=text autocomplete=off spellcheck=false required "
        'placeholder="did:web:…:agents:… or dmd_identity_…">'
        "<div class=actions><button id=find-go>Continue</button>"
        "<button type=button id=deny class=secondary>Cancel</button></div>"
        "<p id=find-error class=status role=alert></p></form>"
        "<div id=sign hidden></div>"
        "<p id=announcer class=visually-hidden aria-live=polite></p>"
        "</section>"
    )
    config = {
        "signIn": sign_in_id,
        "ownerKeyGuide": OWNER_KEY_GUIDE,
        "authServer": issuer(state.settings),
        "gateway": state.settings.public_base_url.rstrip("/"),
        "getDid": brand.PRODUCTION_APP,
    }
    return _page(request, "Sign in with your DID-LLM", body, config)


# --------------------------------------------------------------------------- the page's calls


class DidChoice(BaseModel):
    did: str = Field(min_length=1, max_length=300)
    controller: str | None = Field(default=None, pattern=r"^(wallet|key)$")


class Signature(BaseModel):
    signature: str = Field(min_length=1, max_length=4000)


def _json_error(exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message),
        headers=exc.headers,
    )


def _call(request: Request, work: Any) -> Response:
    if not _enabled(request):
        return _not_found()
    try:
        return JSONResponse(
            work(request.app.state, datetime.now(UTC)), headers={"Cache-Control": "no-store"}
        )
    except ApiError as exc:
        return _json_error(exc)


@router.get(AUTHORIZE_PATH + "/{sign_in_id}/request")
def sign_in_request(request: Request, sign_in_id: str) -> Response:
    def work(state: Any, now: datetime) -> dict[str, Any]:
        with transaction(state.session_factory) as session:
            sign_in = flow.find(session, sign_in_id, now)
            identity = (
                session.get(AgentIdentity, sign_in.identity_id) if sign_in.identity_id else None
            )
            return flow.describe(sign_in, identity)

    return _call(request, work)


@router.post(AUTHORIZE_PATH + "/{sign_in_id}/did")
def sign_in_did(request: Request, sign_in_id: str, body: DidChoice) -> Response:
    controller = {"wallet": flow.EIP155, "key": flow.ED25519}.get(body.controller or "")

    def work(state: Any, now: datetime) -> dict[str, Any]:
        with transaction(state.session_factory) as session:
            sign_in = flow.find(session, sign_in_id, now, lock=True)
            identity, found = flow.choose(
                session,
                sign_in,
                body.did,
                controller,
                issuer=issuer(state.settings),
                now=now,
            )
            answer = flow.describe(sign_in, identity)
            answer["controllers"] = [flow.CONTROLLER_LABELS[kind] for kind in found]
            answer["display_name"] = identity.display_name
            return answer

    return _call(request, work)


@router.post(AUTHORIZE_PATH + "/{sign_in_id}/signature")
def sign_in_signature(request: Request, sign_in_id: str, body: Signature) -> Response:
    def work(state: Any, now: datetime) -> dict[str, Any]:
        with transaction(state.session_factory) as session:
            sign_in = flow.find(session, sign_in_id, now, lock=True)
            verified = flow.approve(
                session,
                sign_in,
                body.signature,
                wallets=state.wallet_verifier,
                signer=state.token_signer,
                issuer=issuer(state.settings),
                now=now,
            )
            left = flow.MAX_ATTEMPTS - sign_in.attempts
        if not verified:
            raise ApiError(
                422,
                "invalid_signature",
                "That signature does not match the DID-LLM's owner"
                + (f": {left} tries left." if left > 0 else ": start again from the app."),
            )
        return {"status": "approved"}

    return _call(request, work)


@router.post(AUTHORIZE_PATH + "/{sign_in_id}/finish")
def sign_in_finish(request: Request, sign_in_id: str) -> Response:
    def work(state: Any, now: datetime) -> dict[str, Any]:
        with transaction(state.session_factory) as session:
            sign_in = flow.find(session, sign_in_id, now, lock=True)
            code = flow.finish(session, sign_in, now)
            target = back_to_app(state, sign_in.redirect_uri, sign_in.state, code=code)
        return {"redirect": target}

    return _call(request, work)


@router.post(AUTHORIZE_PATH + "/{sign_in_id}/deny")
def sign_in_deny(request: Request, sign_in_id: str) -> Response:
    def work(state: Any, now: datetime) -> dict[str, Any]:
        with transaction(state.session_factory) as session:
            sign_in = flow.find(session, sign_in_id, now, lock=True)
            flow.deny(sign_in)
            target = back_to_app(
                state,
                sign_in.redirect_uri,
                sign_in.state,
                error="access_denied",
                error_description="the person did not sign in",
            )
        return {"redirect": target}

    return _call(request, work)


# --------------------------------------------------------------------------- revoke


def _sign_in_of(state: Any, token: str, client_id: str, now: datetime) -> str | None:
    """The sign-in an app's access token belongs to, if it is one of ours for that app."""
    try:
        claims = verify_token(
            token,
            state.token_signer.public_keys,
            issuer=issuer(state.settings),
            audiences=list(protected_resources(state.settings).values()),
            now=now,
        )
    except TokenRejected:
        return None  # not ours, or expired: nothing to end (RFC 7009, section 2.2)
    sid = claims.get("sid")
    return str(sid) if sid and claims.get("client_id") == client_id else None


@router.post(REVOKE_PATH)
async def revoke(request: Request) -> Response:
    if not _enabled(request):
        return _not_found()
    fields = await read_form(request)
    credentials = (
        ""
        if isinstance(fields, str)
        else client_credentials(fields, request.headers.get("authorization"))
    )
    if isinstance(fields, str) or not fields.get("token") or isinstance(credentials, str):
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "error_description": "send token and client_id"},
            headers={"Cache-Control": "no-store"},
        )
    state = request.app.state
    client_id, secret = credentials
    if registration.is_registered_id(client_id):
        # A registered app (Google's) authenticates with its secret (RFC 7009 section 2.1).
        try:
            client = await anyio.to_thread.run_sync(state.oauth_clients.get, client_id)
        except ClientRejected:
            client = None
        if client is None or not registration.secret_matches(client, secret):
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_client", "error_description": "unknown app or secret"},
                headers={"Cache-Control": "no-store", "WWW-Authenticate": 'Basic realm="revoke"'},
            )

    def work() -> None:
        now = datetime.now(UTC)
        token = fields["token"]
        with transaction(state.session_factory) as session:
            if flow.revoke(session, token=token, client_id=client_id, now=now):
                return
            # An access token: ends the whole sign-in it belongs to, so that its other tokens
            # stop too (RFC 7009 lets the server choose; partial revocation would leave the
            # refresh token able to get new ones).
            sign_in = _sign_in_of(state, token, client_id, now)
            if sign_in is not None:
                flow.revoke_family(session, sign_in, now)

    await anyio.to_thread.run_sync(work)
    return JSONResponse({}, headers={"Cache-Control": "no-store"})
