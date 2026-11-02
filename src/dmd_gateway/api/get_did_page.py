"""The "Get a DID" page: a DID registration for people, in the debitmydata.com look.

It takes a visitor through the registration's steps (details and quote, payment, proof of
control, the DID), filling each one in from the previous answers. The visitor pays from their
own wallet and signs one message with it (EIP-6963 browser wallets, or a signature pasted from
another wallet or the agent's own tools). The page never asks for a private key or a recovery
phrase and never sends money. It keeps the registration's IDs in the browser so that a reload
continues where it stopped.

On production it is the docs page (/docs), with the Swagger UI page as the advanced option
(/docs/advanced); on every server it is also at /get-did, for links from DebitMyData's sites.
On staging it adds a "Pay with test coins" button. Only its own script and style run (hashes
in the Content-Security-Policy), its font is served here (Mada, SIL Open Font License), and it
may only call this server.
"""

from __future__ import annotations

import base64
import hashlib
import json
from importlib.resources import files

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dmd_gateway.api import docs_pages

router = APIRouter(include_in_schema=False)  # every server
production_router = APIRouter(include_in_schema=False)  # outside staging: /docs is this page

PAGE_PATH = "/get-did"
DOCS_PATH = "/docs"
ADVANCED_PATH = "/docs/advanced"
_STATIC = files("dmd_gateway") / "static"
_STYLE = (_STATIC / "get-did.css").read_text(encoding="utf-8")
_SCRIPT = (_STATIC / "get-did.js").read_text(encoding="utf-8")
FONTS = (
    "mada-latin-400-normal.woff2",
    "mada-latin-600-normal.woff2",
    "mada-latin-700-normal.woff2",
)


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


CSP = (
    f"default-src 'none'; script-src 'sha256-{_sha256(_SCRIPT)}'; "
    f"style-src 'sha256-{_sha256(_STYLE)}'; font-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# What the DID is for: the participant types, in words people use.
PARTICIPANTS = (
    ("ai_agent", "An AI agent"),
    ("autonomous_agent", "An autonomous agent"),
    ("bot", "A bot"),
    ("agenticbot", "An agentic bot"),
    ("claw", "A claw"),
    ("digital_twin", "A digital twin"),
    ("human", "A person (myself)"),
    ("organization", "An organization"),
    ("approved_digital_entity", "Another approved digital entity"),
)


def _body(staging: bool) -> str:
    participants = "".join(
        f"<option value={value}>{label}</option>" for value, label in PARTICIPANTS
    )
    modes = (
        '<span class=on aria-current=page>Get a DID</span><a href="/docs">Guided test</a>'
        if staging
        else "<span class=on aria-current=page>Get a DID</span>"
    ) + f'<a href="{ADVANCED_PATH}">Advanced: API docs</a>'
    staging_note = (
        "<p class=staging><strong>Staging server.</strong> Test networks and test DIDs: pay "
        "with the <strong>Pay with test coins</strong> button, and sign with your own wallet "
        "(signing sends nothing).</p>"
        if staging
        else ""
    )
    return f"""
<div id=countdown hidden aria-hidden=true><span class=label></span><span class=time></span></div>
<header class=topbar><div class=in>
<a class=brand href="https://debitmydata.com/"><img src="/favicon.png" alt="">DebitMyData</a>
<nav class=modes aria-label="Ways to register">{modes}</nav>
</div></header>
<main>
<section class=hero>
<h1>Get your DebitMyData DID</h1>
<p>A DID is a verifiable identity for an AI agent, a bot, a person or an organization: a
DID document anyone can check, a credential signed by DebitMyData, and the DID holder
entitlement.</p>
<span class=price id=price>$25 USD, paid once</span>
</section>
{staging_note}
<p class=safe><strong>Safe by design:</strong> this page never asks for a private key or a
recovery phrase, and never sends money. You pay and sign in your own wallet.</p>
<div id=resume class=resume hidden aria-live=polite></div>

<section class="card step current" id=s1>
<div class=step-head><span class=num>1</span><h2>About your DID</h2></div>
<div class=body>
<form id=details novalidate>
<label class=field for=name>Name<small>Shown in the DID document, e.g. the agent's name.</small>
<input type=text id=name maxlength=100 autocomplete=off
placeholder="e.g. My research agent"></label>
<label class=field for=kind>What is it for?<select id=kind>{participants}</select></label>
<fieldset>
<legend>Who controls it</legend>
<label class=choice><input type=radio name=controller value=wallet checked>
<strong>My wallet</strong><span class=tag>Recommended</span>
<small>A browser wallet such as MetaMask or Coinbase Wallet, or any Ethereum-style address
(0x…). You will sign one message with it.</small></label>
<div id=wallet-box class=sub>
<div class=wallets-holder></div>
<label class=field for=wallet-address>Or type the wallet address
<input type=text id=wallet-address autocomplete=off spellcheck=false placeholder="0x…"></label>
</div>
<label class=choice><input type=radio name=controller value=key>
<strong>My agent's public key</strong>
<small>For developers: the agent's Ed25519 public key. You will sign one message with the
agent's key.</small></label>
<div id=key-box class=sub hidden>
<label class=field for=public-key>Public key
<input type=text id=public-key autocomplete=off spellcheck=false
placeholder="ed25519:z6Mk…"></label>
</div>
</fieldset>
<fieldset>
<legend>How to pay</legend>
<div id=options><p class=small>Loading the payment options…</p></div>
</fieldset>
<label class=check><input type=checkbox id=terms><span>I accept the
<a href="/policies/terms" target=_blank>terms</a> and the
<a href="/policies/payment-methods" target=_blank>payment policy</a>, and I understand that a
DID registration is <a href="/policies/refunds" target=_blank>not refundable</a>.</span></label>
<p class=error id=details-error hidden role=alert></p>
<button type=submit id=get-quote>Get my quote</button>
</form>
</div>
</section>
<section class="card step waiting" id=s2>
<div class=step-head><span class=num>2</span><h2>Pay</h2></div><div class=body></div>
</section>
<section class="card step waiting" id=s3>
<div class=step-head><span class=num>3</span><h2>Prove it's yours</h2></div><div class=body></div>
</section>
<section class="card step waiting" id=s4>
<div class=step-head><span class=num>4</span><h2>Your DID</h2></div><div class=body></div>
</section>
<p id=announcer class=sr-only aria-live=polite></p>
</main>
<footer><div class=in>
<a href="/policies/terms">Terms</a><a href="/policies/privacy">Privacy</a>
<a href="/policies/refunds">Refunds</a><a href="/policies/payment-methods">Payment methods</a>
<a href="/policies/security">Security</a><a href="{ADVANCED_PATH}">API docs (advanced)</a>
<a href="https://debitmydata.com/">debitmydata.com</a>
</div></footer>
""".strip()


def page_html(staging: bool) -> str:
    config = json.dumps({"staging": staging}).replace("<", "\\u003c")
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Get your DID · DebitMyData</title>"
        '<link rel=icon href="/favicon.png">'
        f"<style>{_STYLE}</style></head><body>{_body(staging)}"
        f'<script type="application/json" id="page-config">{config}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


def _page(request: Request) -> HTMLResponse:
    staging = bool(request.app.state.settings.staging_test_tools)
    return HTMLResponse(page_html(staging), headers=HEADERS)


router.add_api_route(PAGE_PATH, _page, methods=["GET"])


@router.get("/fonts/{name}")
def font(name: str) -> Response:
    if name not in FONTS:
        raise HTTPException(status_code=404)
    return Response(
        (_STATIC / "fonts" / name).read_bytes(),
        media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"},
    )


production_router.add_api_route(DOCS_PATH, _page, methods=["GET"])
production_router.add_api_route(ADVANCED_PATH, docs_pages.swagger_ui, methods=["GET"])


@production_router.get("/")
def to_docs() -> RedirectResponse:
    return RedirectResponse(DOCS_PATH, status_code=307)
