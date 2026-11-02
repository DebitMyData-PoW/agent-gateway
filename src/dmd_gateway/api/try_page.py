"""Staging only: /try, a guided page that runs the whole test registration.

It calls the same API as an agent, in order, and fills each step in from the previous
results, so a visitor only clicks Execute (or "Run all steps"). A floating countdown shows
the time left on the quote, the controller challenge and, for USDC, the signed payment.
The page's own style and script are the only ones allowed to run (hashes in the CSP), and it
may only call this server.
"""

from __future__ import annotations

import base64
import hashlib
from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)

TRY_PATH = "/try"
_STATIC = files("dmd_gateway") / "static"
_STYLE = (_STATIC / "try.css").read_text(encoding="utf-8")
_SCRIPT = (_STATIC / "try.js").read_text(encoding="utf-8")


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


CSP = (
    f"default-src 'none'; script-src 'sha256-{_sha256(_SCRIPT)}'; "
    f"style-src 'sha256-{_sha256(_STYLE)}'; connect-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_BODY = """
<div id=countdown hidden aria-hidden=true><span class=label></span><span class=time></span></div>
<main>
<header class=top><img src="/favicon.png" alt=""><h1>Try a DID registration</h1></header>
<p class=lead>This page runs every step of a registration for you, the way an AI agent does
it through the API. Click <strong>Execute</strong> on each step, or <strong>Run all
steps</strong>.</p>
<p class=note><strong>Staging server.</strong> Payments use test networks whose coins have no
value, and the DID you get is a test DID. The test agent's private key never leaves this
page. Developers: the same steps one by one are on the <a href="/docs">API docs</a> page.</p>
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
<div class=actions>
<button type=button id=run-all>Run all steps</button>
<button type=button id=restart class=secondary>Start over</button>
</div>
<ol id=steps class=steps></ol>
<section id=result class=result hidden aria-live=polite></section>
<p id=announcer class=sr-only aria-live=polite></p>
</main>
""".strip()


def page_html() -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Try a DID registration · DebitMyData Agent Gateway</title>"
        '<link rel=icon href="/favicon.png">'
        f"<style>{_STYLE}</style></head><body>{_BODY}"
        f"<script>{_SCRIPT}</script></body></html>"
    )


@router.get(TRY_PATH)
def try_page() -> HTMLResponse:
    return HTMLResponse(page_html(), headers=HEADERS)
