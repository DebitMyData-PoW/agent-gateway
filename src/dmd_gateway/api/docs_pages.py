"""The interactive API docs (/docs, /redoc), the DebitMyData favicon and the logo in the
pages' top bar.

FastAPI's built-in docs pages are switched off in ``create_app`` and served here instead,
so that they carry the DebitMyData logo and open with "Try it out" already enabled. The
Swagger UI page is the advanced option at /docs/advanced: /docs is the guided test page on
staging (``try_page``) and the "Get a DID-LLM" page elsewhere (``get_did_page``).

Both pages load Swagger UI or ReDoc from jsDelivr at an exact version, and the browser checks
each file against its SHA-384 fingerprint before running it (Subresource Integrity). Their
Content-Security-Policy lets nothing else run: only those files, the page's own script and
style (by hash), and requests to this server. To move to another version, change the version
below and both fingerprints, from:

    curl -sL <file URL> | openssl dgst -sha384 -binary | openssl base64 -A

The Swagger UI page also keeps the values each answer gives (IDs, the payment address and
amount, the message to sign, signatures, the DID-LLM links) in a panel, each with its own Copy
button and the step it goes into (``static/docs-helper.js``). It keeps them in the open page
only: nothing is stored or sent anywhere.

The Swagger UI page has the debitmydata.com look of the gateway's other pages (``brand``):
their top bar and footer, the Mada font, and red for the endpoints that act (POST), grey for
those that only read (GET). It opens like the guided pages: what the page is for, then the
registration's steps as cards (the next one with what to paste into it and a button that
opens its endpoint), the full written guide folded away, and every endpoint below, with
bigger text, fields and buttons than Swagger UI's own.
"""

from __future__ import annotations

import base64
import hashlib
import json
from html import escape
from importlib.resources import files
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from dmd_gateway.api import brand

router = APIRouter(include_in_schema=False)

_STATIC = files("dmd_gateway") / "static"
_ICONS = {
    "favicon.ico": "image/x-icon",
    "favicon.png": "image/png",
    "logo.png": "image/png",
    "logo-dark.png": "image/png",
}
FAVICON_URL = "/favicon.png"

_CDN = "https://cdn.jsdelivr.net/npm"
SWAGGER_UI_VERSION = "5.33.3"
SWAGGER_UI_JS = (
    f"{_CDN}/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui-bundle.js",
    "sha384-h00llMzD1afyNow1pDmnZKlHsUZgp9KE9HXLFXF2CvbwsBY0+WBO4f75SfGwvM4K",
)
SWAGGER_UI_CSS = (
    f"{_CDN}/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui.css",
    "sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW",
)
REDOC_VERSION = "2.5.4"
REDOC_JS = (
    f"{_CDN}/redoc@{REDOC_VERSION}/bundles/redoc.standalone.js",
    "sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5",
)

# Swagger UI options: links to each endpoint (#/<section>/<operation>), "Try it out" already
# on, request durations shown, the schema list at the bottom collapsed, and no badge from
# the public validator (it would send this API's address to swagger.io).
SWAGGER_UI_PARAMETERS = {
    "dom_id": "#swagger-ui",
    "layout": "BaseLayout",
    "deepLinking": True,
    "showExtensions": True,
    "showCommonExtensions": True,
    "tryItOutEnabled": True,
    "displayRequestDuration": True,
    "defaultModelsExpandDepth": 0,
    "docExpansion": "list",
    "validatorUrl": None,
}

_HELPER_JS = (_STATIC / "docs-helper.js").read_text(encoding="utf-8")
_HELPER_CSS = brand.CSS + (_STATIC / "docs-helper.css").read_text(encoding="utf-8")
_REDOC_CSS = "body{margin:0;padding:0}"


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


_COMMON_CSP = (
    "default-src 'none'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
SWAGGER_CSP = (
    f"script-src {SWAGGER_UI_JS[0]} 'sha256-{_sha256(_HELPER_JS)}' {brand.THEME_SCRIPT_SOURCE}; "
    f"style-src {SWAGGER_UI_CSS[0]} 'sha256-{_sha256(_HELPER_CSS)}'; font-src 'self'; "
    f"{_COMMON_CSP}"
)
# ReDoc styles itself with <style> elements it writes at run time, searches in a worker, and
# shows the small Redocly logo from Redocly's site.
REDOC_LOGO = "https://cdn.redoc.ly/redoc/logo-mini.svg"
REDOC_CSP = (
    f"script-src {REDOC_JS[0]}; style-src 'unsafe-inline'; worker-src blob:; "
    + _COMMON_CSP.replace("img-src 'self' data:", f"img-src 'self' data: {REDOC_LOGO}")
)
_HEADERS = {
    "Cache-Control": brand.NO_TRANSFORM,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _external(tag: str, url: str, integrity: str) -> str:
    attribute = "href" if tag == "link" else "src"
    extra = ' rel="stylesheet"' if tag == "link" else ""
    end = "" if tag == "link" else "</script>"
    return (
        f'<{tag}{extra} {attribute}="{url}" integrity="{integrity}" crossorigin="anonymous">{end}'
    )


def _intro(staging: bool, version: str) -> str:
    """The top of the page: what it is for, the easier pages, and the steps' cards (drawn by
    docs-helper.js in #adv-steps)."""
    if staging:
        note = (
            "<p class=adv-note><strong>Staging server.</strong> Test networks whose coins have "
            "no value, and test DID-LLMs. The three <strong>staging</strong> endpoints stand in "
            "for what a real agent does with its own key and wallet.</p>"
        )
        buttons = (
            f'<a class="adv-button" href="{brand.GUIDED_PATH}">Easiest: the guided test</a>'
            f'<a class="adv-button secondary" href="{brand.GET_DID_PATH}">Get a DID-LLM with your '
            "wallet</a>"
            f'<a class="adv-button secondary" href="{brand.PRODUCTION_SITE}{brand.ADVANCED_PATH}">'
            "The real API (production)</a>"
        )
    else:
        note = (
            "<p class=adv-safe><strong>Every step here is real:</strong> quotes, payments and "
            "DID-LLMs. Paying and signing happen outside this page, in your own wallet and tools. "
            "Never paste a private key or a recovery phrase into this page: the gateway never "
            "asks for one.</p>"
        )
        buttons = (
            f'<a class="adv-button" href="{brand.GET_DID_PATH}">Easiest: Get a DID-LLM</a>'
            f'<a class="adv-button secondary" href="{brand.STAGING_GUIDED_PAGE}">Try it first '
            "with test coins</a>"
        )
    return f"""<main class=adv>
<section class=adv-hero>
<h1>Advanced: API docs</h1>
<p class=adv-lead>Every endpoint of the DebitMyData Agent Gateway, to call by hand: open one,
fill in its fields and click <strong>Execute</strong>. AI agents use the same API, or the MCP
server at <code>/agenticsuperhighway</code>.</p>
{note}
<div class=adv-actions>{buttons}</div>
<p class=adv-meta>Version {escape(version)} · <a href="/openapi.json">OpenAPI (JSON)</a> ·
<a href="/redoc">ReDoc</a> · <a href="{brand.POLICIES_PATH}">Policies</a></p>
</section>
<section class=adv-section aria-labelledby=adv-steps-title>
<h2 id=adv-steps-title>Register a DID-LLM, step by step</h2>
<p class=adv-sub>The next step is in red: open it, paste what it needs, click
<strong>Execute</strong>. The panel at the bottom right keeps every value the answers give, with
a <strong>Copy</strong> button.</p>
<div id=adv-steps></div>
<details class=adv-guide id=adv-guide><summary>The full written guide: every detail</summary>
<div class=adv-guide-body><p class=adv-sub>Loading…</p></div></details>
</section>
<section class=adv-section aria-labelledby=adv-all-title>
<h2 id=adv-all-title>All endpoints</h2>
<p class=adv-sub><span class="adv-method get">GET</span> only reads,
<span class="adv-method post">POST</span> does something. Click an endpoint to open it, fill in
its fields, then click <strong>Execute</strong>.</p>
</section>
</main>"""


def swagger_html(
    openapi_url: str,
    title: str,
    steps: dict[str, Any] | None = None,
    staging: bool = False,
    version: str = "",
) -> str:
    settings = {"swagger": {"url": openapi_url, **SWAGGER_UI_PARAMETERS}, **(steps or {})}
    config = json.dumps(settings, indent=1)
    config = config.replace("<", "\\u003c")  # data block: nothing in it can close the tag
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        f'<link rel="icon" href="{FAVICON_URL}">'
        f"{brand.THEME_SCRIPT}{_external('link', *SWAGGER_UI_CSS)}"
        f"<style>{_HELPER_CSS}</style></head>"
        f"<body>{brand.header('advanced', staging)}{_intro(staging, version)}"
        '<div id="swagger-ui"></div>'
        f"{brand.footer(staging, to_top=False)}"
        f'<script type="application/json" id="swagger-config">{config}</script>'
        f"{_external('script', *SWAGGER_UI_JS)}<script>{_HELPER_JS}</script></body></html>"
    )


def swagger_ui(request: Request) -> HTMLResponse:
    steps = getattr(request.app.state, "docs_steps", None)  # set by create_app
    html = swagger_html(
        request.app.openapi_url,
        f"{request.app.title} - API docs",
        steps() if steps else None,
        staging=bool(request.app.state.settings.staging_test_tools),
        version=request.app.version,
    )
    return HTMLResponse(html, headers={**_HEADERS, "Content-Security-Policy": SWAGGER_CSP})


@router.get("/redoc")
def redoc(request: Request) -> HTMLResponse:
    title = escape(f"{request.app.title} - API reference")
    html = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title><link rel="icon" href="{FAVICON_URL}">'
        f"<style>{_REDOC_CSS}</style></head><body>"
        "<noscript>ReDoc needs JavaScript to show the API reference.</noscript>"
        f'<redoc spec-url="{escape(request.app.openapi_url)}"></redoc>'
        f"{_external('script', *REDOC_JS)}</body></html>"
    )
    return HTMLResponse(html, headers={**_HEADERS, "Content-Security-Policy": REDOC_CSP})


def _icon(name: str) -> Response:
    return Response(
        (_STATIC / name).read_bytes(),
        media_type=_ICONS[name],
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.ico")
def favicon_ico() -> Response:
    return _icon("favicon.ico")


@router.get("/favicon.png")
def favicon_png() -> Response:
    return _icon("favicon.png")


# The DebitMyData logo (the coin side of debitmydata.com's logo) in every page's top bar.
@router.get("/logo.png")
def logo_png() -> Response:
    return _icon("logo.png")


# The same logo for the dark theme: its binary lines red instead of black, as on the app's icons.
@router.get("/logo-dark.png")
def logo_dark_png() -> Response:
    return _icon("logo-dark.png")
