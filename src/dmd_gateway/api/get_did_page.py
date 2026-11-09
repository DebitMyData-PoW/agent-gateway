"""The "Get a DID" page: a DID registration for people, in the debitmydata.com look.

It takes a visitor through the registration's steps (details and quote, payment, proof of
control, the DID), filling each one in from the previous answers. The visitor pays from their
own wallet and signs one message with it (EIP-6963 browser wallets, or a signature pasted from
another wallet or the agent's own tools). The page never asks for a private key or a recovery
phrase and never sends money. It keeps the registration's IDs in the browser so that a reload
continues where it stopped.

For a DMD payment, step 2 also shows the payment as a QR code and an "Open in my DMD wallet"
button: the payment link ``debitmydata:ADDRESS?amount=AMOUNT``, the link DMD wallets open
(DMD Core registers that prefix with the system and reads the address and the amount from it).

It is at /get-did on every server, the address to link from DebitMyData's sites; on
production the docs address (/docs) and the site root lead there too, and the Swagger UI page
is the advanced option (/docs/advanced). On staging it adds a "Pay with test coins" button.
Only its own script and style run (hashes in the Content-Security-Policy), its font is served
here (Mada, SIL Open Font License; the look is shared with the other pages, ``brand``), and
it may only call this server.

The installable app: on the host named by the ``app_host`` setting (id.debitmydata.io), the
same page is served at the root, with a web app manifest, icons and a small service worker, so
that people can add it to their phone's home screen and open it full screen, like an app (iPhone:
Share, Add to Home Screen; Android: Install app). The service worker only keeps the last copy
of the page, its font and icons, for opening without a network: it never touches the API calls.
The app's identity is its address, so the host must not change once people have installed it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from importlib.resources import files

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dmd_gateway.api import brand, docs_pages

router = APIRouter(include_in_schema=False)  # every server
production_router = APIRouter(include_in_schema=False)  # outside staging: /docs leads here

PAGE_PATH = brand.GET_DID_PATH
DOCS_PATH = "/docs"
ADVANCED_PATH = brand.ADVANCED_PATH
_STATIC = files("dmd_gateway") / "static"
_STYLE = brand.CSS + (_STATIC / "get-did.css").read_text(encoding="utf-8")
# The QR code encoder (qr.js) runs first: step 2 draws the DMD payment link with it.
_SCRIPT = "\n".join(
    (_STATIC / name).read_text(encoding="utf-8") for name in ("qr.js", "get-did.js")
)
_WORKER = (_STATIC / "get-did-sw.js").read_text(encoding="utf-8")
FONTS = (
    "mada-latin-400-normal.woff2",
    "mada-latin-600-normal.woff2",
    "mada-latin-700-normal.woff2",
)


# The installable app (on the app host only).
APP_ROOT = "/"
MANIFEST_PATH = "/manifest.webmanifest"
WORKER_PATH = "/sw.js"
APP_NAME = "DebitMyData DID"
APP_SHORT_NAME = "DebitMyData"
THEME_COLOR = "#640000"  # the top bar's red
APP_ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png")
DESCRIPTION = (
    "Get your DebitMyData DID: a verifiable identity for an AI agent, a bot, a person or an "
    "organization, with a credential signed by DebitMyData."
)


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


CSP = (
    f"default-src 'none'; script-src 'sha256-{_sha256(_SCRIPT)}'; "
    f"style-src 'sha256-{_sha256(_STYLE)}'; font-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
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


# What DMD is and how to get some: opened from the "?" under the DMD payment option.
_GET_DMD = """<div id=get-dmd class=explain hidden>
<h3>About DMD</h3>
<p>DMD is the native coin of the DebitMyData Proof-of-Work chain. Here it is simply one way to
pay for your DID: a payment method, not an investment.</p>
<h4>How to get DMD</h4>
<ol>
<li><strong>Mine it.</strong> Point SHA-256 mining hardware, your own or rented by the hour, at
the official pool, <a href="https://pool.debitmydata.io/" target=_blank
rel=noopener>pool.debitmydata.io</a>, which pays the DMD into your wallet. How much you mine
depends on your hashrate and the network's. Guides:
<a href="https://debitmydata.io/#asics" target=_blank rel=noopener>mining</a> and
<a href="https://debitmydata.io/#rent" target=_blank rel=noopener>renting hashrate</a>.</li>
<li><strong>Or pay with USDC</strong>, the US-dollar stablecoin, once it opens on this page.</li>
</ol>
<p>To hold and send DMD you need a DMD wallet:
<a href="https://debitmydata.io/#wallet" target=_blank rel=noopener>get one at
debitmydata.io</a>. DMD is not listed on any exchange yet.</p>
</div>"""


def _body(staging: bool, app: bool = False) -> str:
    participants = "".join(
        f"<option value={value}>{label}</option>" for value, label in PARTICIPANTS
    )
    staging_note = (
        "<p class=staging><strong>Staging server.</strong> Test networks and test DIDs: pay "
        "with the <strong>Pay with test coins</strong> button, and sign with your own wallet "
        "(signing sends nothing).</p>"
        if staging
        else ""
    )
    return f"""
<div id=countdown hidden aria-hidden=true><span class=label></span><span class=time></span></div>
{brand.header("get-did", staging, APP_ROOT if app else PAGE_PATH)}
<main>
<section class=hero>
<p class=eyebrow>Verifiable identity</p>
<h1>Get your <span class=accent>DebitMyData</span> DID</h1>
<p>A DID is a verifiable identity for an AI agent, a bot, a person or an organization: a
DID document anyone can check, a credential signed by DebitMyData, and the DID holder
entitlement.</p>
<ul class=perks><li>Signed by DebitMyData</li><li>Checkable by anyone</li><li>No account
needed</li></ul>
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
<legend>Who owns it</legend>
<p class=small>Every DID needs an owner: a wallet or an agent's key. The owner proves it once
by signing one message (free: signing sends nothing).</p>
<label class=choice><input type=radio name=controller value=wallet checked>
<strong>My wallet</strong><span class=tag>Recommended</span>
<small>Any Ethereum-style wallet, whose address starts with 0x: MetaMask, Coinbase Wallet,
Rabby, Trust Wallet, a hardware wallet… It needs no balance (no USDC, no ETH): it only signs,
nothing is paid with it. No browser extension needed: you can type the address and sign in
your wallet app. You pay in DMD at step 2, with any DMD wallet.</small></label>
<div id=wallet-box class=sub>
<div class=wallets-holder></div>
<label class=field for=wallet-address><span id=wallet-address-label>Or type the wallet's
address (0x…)</span>
<input type=text id=wallet-address autocomplete=off spellcheck=false placeholder="0x…"></label>
<p class=hint id=wallet-hint hidden aria-live=polite></p>
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
{_GET_DMD}
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
<div class=step-head><span class=num>2</span><h2>Pay</h2></div>
<p class="small locked">Opens after <strong>Get my quote</strong> at step 1: you then pay in DMD,
with any DMD wallet.</p><div class=body></div>
</section>
<section class="card step waiting" id=s3>
<div class=step-head><span class=num>3</span><h2>Prove it's yours</h2></div>
<p class="small locked">Opens once the payment is confirmed.</p><div class=body></div>
</section>
<section class="card step waiting" id=s4>
<div class=step-head><span class=num>4</span><h2>Your DID</h2></div>
<p class="small locked">Opens once the owner has signed.</p><div class=body></div>
</section>
<p id=announcer class=sr-only aria-live=polite></p>
</main>
{brand.footer(staging, APP_ROOT if app else PAGE_PATH)}
""".strip()


def page_html(staging: bool, app: bool = False) -> str:
    config = json.dumps({"staging": staging, "app": app}).replace("<", "\\u003c")
    installable = (
        f'<link rel=manifest href="{MANIFEST_PATH}">'
        f'<meta name=theme-color content="{THEME_COLOR}">'
        "<meta name=mobile-web-app-capable content=yes>"
        "<meta name=apple-mobile-web-app-capable content=yes>"
        f'<meta name=apple-mobile-web-app-title content="{APP_NAME}">'
        "<meta name=apple-mobile-web-app-status-bar-style content=black>"
        '<link rel=apple-touch-icon href="/app/apple-touch-icon.png">'
        if app
        else ""
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Get your DID · DebitMyData</title>"
        f'<meta name=description content="{DESCRIPTION}">'
        '<link rel=icon href="/favicon.png">'
        f"{installable}"
        f"<style>{_STYLE}</style></head><body>{_body(staging, app)}"
        f'<script type="application/json" id="page-config">{config}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


def is_app(request: Request) -> bool:
    """Whether the request came to the app's host (nginx passes the visitor's Host)."""
    host = request.app.state.settings.app_host
    return bool(host) and request.headers.get("host", "").strip().lower() == host


def _render(request: Request, app: bool) -> HTMLResponse:
    staging = bool(request.app.state.settings.staging_test_tools)
    return HTMLResponse(page_html(staging, app), headers=HEADERS)


def _page(request: Request) -> Response:
    if is_app(request):  # one address for the app: its root
        return RedirectResponse(APP_ROOT, status_code=307)
    return _render(request, app=False)


def _root(request: Request) -> Response:
    """The site root: the app on the app's host; elsewhere the guided test (staging) or this
    page (production)."""
    if is_app(request):
        return _render(request, app=True)
    if request.app.state.settings.staging_test_tools:
        return RedirectResponse(brand.GUIDED_PATH, status_code=307)
    return RedirectResponse(PAGE_PATH, status_code=307)


router.add_api_route(PAGE_PATH, _page, methods=["GET"])
router.add_api_route(APP_ROOT, _root, methods=["GET"])


def _app_only(request: Request) -> None:
    # Nowhere else: an installed app is tied to one address.
    if not is_app(request):
        raise HTTPException(status_code=404)


def manifest_json() -> dict[str, object]:
    icons = [
        {"src": "/app/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/app/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {
            "src": "/app/icon-maskable-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ]
    return {
        "id": APP_ROOT,
        "name": APP_NAME,
        "short_name": APP_SHORT_NAME,
        "description": DESCRIPTION,
        "lang": "en",
        "start_url": APP_ROOT,
        "scope": APP_ROOT,
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": THEME_COLOR,
        "categories": ["business", "utilities"],
        "icons": icons,
    }


@router.get(MANIFEST_PATH)
def manifest(request: Request) -> Response:
    _app_only(request)
    return Response(
        json.dumps(manifest_json()),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@router.get(WORKER_PATH)
def service_worker(request: Request) -> Response:
    _app_only(request)
    return Response(
        _WORKER,
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-cache",  # a new version reaches installed apps at once
            "Content-Security-Policy": "default-src 'none'; connect-src 'self'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/app/{name}")
def app_icon(name: str, request: Request) -> Response:
    _app_only(request)
    if name not in APP_ICONS:
        raise HTTPException(status_code=404)
    return Response(
        (_STATIC / "app" / name).read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/fonts/{name}")
def font(name: str) -> Response:
    if name not in FONTS:
        raise HTTPException(status_code=404)
    return Response(
        (_STATIC / "fonts" / name).read_bytes(),
        media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"},
    )


production_router.add_api_route(ADVANCED_PATH, docs_pages.swagger_ui, methods=["GET"])


def _to_page() -> RedirectResponse:
    return RedirectResponse(PAGE_PATH, status_code=307)


# The site root is _root above (it also serves the app on the app's host).
production_router.add_api_route(DOCS_PATH, _to_page, methods=["GET"])
