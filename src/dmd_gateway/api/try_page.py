"""Staging only: the guided test page at /docs, which runs the whole test registration.

It calls the same API as an agent, in order, and fills each step in from the previous
results, so a visitor only clicks Execute (or "Run all steps"). A floating countdown shows
the time left on the quote, the controller challenge and, for USDC, the signed payment; for
USDC it first checks that the test wallet holds enough test USDC, and shows how to add some.
The page's own style and script are the only ones allowed to run (hashes in the CSP), and it
may only call this server. It has the debitmydata.com look of the gateway's other pages
(``brand``).

The Swagger UI page, for doing the steps by hand and seeing every endpoint, is the advanced
option at /docs/advanced. The first address of this page (/try) and the site root lead here.
"""

from __future__ import annotations

import base64
import hashlib
from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dmd_gateway.api import brand, docs_pages

router = APIRouter(include_in_schema=False)

PAGE_PATH = brand.GUIDED_PATH
ADVANCED_PATH = brand.ADVANCED_PATH
REDIRECTS = ("/try",)  # the site root: get_did_page._root
_STATIC = files("dmd_gateway") / "static"
_STYLE = brand.CSS + (_STATIC / "try.css").read_text(encoding="utf-8")
_SCRIPT = (_STATIC / "try.js").read_text(encoding="utf-8")


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


CSP = (
    f"default-src 'none'; script-src 'sha256-{_sha256(_SCRIPT)}'; "
    f"style-src 'sha256-{_sha256(_STYLE)}'; font-src 'self'; connect-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Cache-Control": f"no-store, {brand.NO_TRANSFORM}",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_COUNTDOWN = (
    "<div id=countdown hidden aria-hidden=true>"
    "<span class=label></span><span class=time></span></div>"
)
_BODY = """
<main>
<h1>Try a DID registration</h1>
<p class=lead>This page runs every step of a registration for you, the way an AI agent does
it through the API. Click <strong>Execute</strong> on each step, or <strong>Run all
steps</strong>.</p>
<p class=note><strong>Staging server.</strong> Payments use test networks whose coins have no
value, and the DID you get is a test DID. The test agent's private key never leaves this
page. To try it as a person, with your own wallet, use <a href="/get-did">Get a DID</a>.
Developers: the same steps by hand, and every other endpoint, are in the
<a href="/docs/advanced">advanced API docs</a>.</p>
<fieldset>
<legend>Your test registration</legend>
<label class=field for=display-name>Display name of the test agent
<input type=text id=display-name maxlength=100 value="Staging test agent" autocomplete=off>
</label>
<div class=choices role=radiogroup aria-label="Payment">
<label class=choice><input type=radio name=option value=dmd_pow_native checked>
<strong>DMD, the DebitMyData coin</strong>
<small>Test coins on the private regtest chain. 8 steps.</small></label>
<label class=choice><input type=radio name=option value=x402_usdc_base>
<strong>USDC through x402</strong>
<small>Test USDC on Base Sepolia. 7 steps.</small></label>
</div>
</fieldset>
<section id=funding class=funding hidden aria-live=polite></section>
<div class=actions>
<button type=button id=run-all>Run all steps</button>
<button type=button id=restart class=secondary>Start over</button>
<span id=run-note class=run-note aria-live=polite></span>
</div>
<ol id=steps class=steps></ol>
<section id=result class=result hidden aria-live=polite></section>
<p id=announcer class=sr-only aria-live=polite></p>
</main>
"""


def _body() -> str:
    return brand.header("guided", staging=True) + _BODY + brand.footer(staging=True)


def page_html() -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Try a DID registration · DebitMyData Agent Gateway</title>"
        '<link rel=icon href="/favicon.png">'
        f"<style>{_STYLE}</style></head><body>{_COUNTDOWN}{_body()}"
        f"<script>{_SCRIPT}</script></body></html>"
    )


@router.get(PAGE_PATH)
def guided_page() -> HTMLResponse:
    return HTMLResponse(page_html(), headers=HEADERS)


@router.get(ADVANCED_PATH)
def advanced_docs(request: Request) -> HTMLResponse:
    return docs_pages.swagger_ui(request)


def _to_page() -> RedirectResponse:
    return RedirectResponse(PAGE_PATH, status_code=307)


for _path in REDIRECTS:
    router.add_api_route(_path, _to_page, methods=["GET"])
