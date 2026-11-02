"""The interactive API docs (/docs, /redoc) and the DebitMyData favicon.

FastAPI's built-in docs pages are switched off in ``create_app`` and served here instead,
so that they carry the DebitMyData logo and open with "Try it out" already enabled. The
Swagger UI page is at /docs, except on staging, where /docs is the guided test page and this
page is the advanced option at /docs/advanced (see ``try_page``).

Both pages load Swagger UI or ReDoc from jsDelivr at an exact version, and the browser checks
each file against its SHA-384 fingerprint before running it (Subresource Integrity). Their
Content-Security-Policy lets nothing else run: only those files, the page's own script and
style (by hash), and requests to this server. To move to another version, change the version
below and both fingerprints, from:

    curl -sL <file URL> | openssl dgst -sha384 -binary | openssl base64 -A

The Swagger UI page also keeps the values each answer gives (IDs, the payment address and
amount, the message to sign, signatures, the DID links) in a panel, each with its own Copy
button and the step it goes into (``static/docs-helper.js``). It keeps them in the open page
only: nothing is stored or sent anywhere.
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

router = APIRouter(include_in_schema=False)
swagger_router = APIRouter(include_in_schema=False)  # GET /docs, outside staging

_STATIC = files("dmd_gateway") / "static"
_ICONS = {
    "favicon.ico": "image/x-icon",
    "favicon.png": "image/png",
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
_HELPER_CSS = (_STATIC / "docs-helper.css").read_text(encoding="utf-8")
_REDOC_CSS = "body{margin:0;padding:0}"


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


_COMMON_CSP = (
    "default-src 'none'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
SWAGGER_CSP = (
    f"script-src {SWAGGER_UI_JS[0]} 'sha256-{_sha256(_HELPER_JS)}'; "
    f"style-src {SWAGGER_UI_CSS[0]} 'sha256-{_sha256(_HELPER_CSS)}'; {_COMMON_CSP}"
)
# ReDoc styles itself with <style> elements it writes at run time, searches in a worker, and
# shows the small Redocly logo from Redocly's site.
REDOC_LOGO = "https://cdn.redoc.ly/redoc/logo-mini.svg"
REDOC_CSP = (
    f"script-src {REDOC_JS[0]}; style-src 'unsafe-inline'; worker-src blob:; "
    + _COMMON_CSP.replace("img-src 'self' data:", f"img-src 'self' data: {REDOC_LOGO}")
)
_HEADERS = {
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


def swagger_html(openapi_url: str, title: str, steps: dict[str, Any] | None = None) -> str:
    settings = {"swagger": {"url": openapi_url, **SWAGGER_UI_PARAMETERS}, **(steps or {})}
    config = json.dumps(settings, indent=1)
    config = config.replace("<", "\\u003c")  # data block: nothing in it can close the tag
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        f'<link rel="icon" href="{FAVICON_URL}">'
        f"{_external('link', *SWAGGER_UI_CSS)}<style>{_HELPER_CSS}</style></head>"
        '<body><div id="swagger-ui"></div>'
        f'<script type="application/json" id="swagger-config">{config}</script>'
        f"{_external('script', *SWAGGER_UI_JS)}<script>{_HELPER_JS}</script></body></html>"
    )


def swagger_ui(request: Request) -> HTMLResponse:
    steps = getattr(request.app.state, "docs_steps", None)  # set by create_app
    html = swagger_html(
        request.app.openapi_url, f"{request.app.title} - API docs", steps() if steps else None
    )
    return HTMLResponse(html, headers={**_HEADERS, "Content-Security-Policy": SWAGGER_CSP})


swagger_router.add_api_route("/docs", swagger_ui, methods=["GET"])


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
