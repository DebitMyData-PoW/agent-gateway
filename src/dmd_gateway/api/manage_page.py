"""The "Manage your DID-LLM" page (/manage), for owners, in the debitmydata.com look:

* **change its owners**: move the DID-LLM to a new wallet or key, add one next to the current
  owner, or drop one. A current owner approves and each new owner accepts, by signing;
* **revoke it for good**, with one message its owner signs.

Wallets sign on the page (EIP-6963 browser wallets, or a signature pasted from a wallet app);
keys sign with the owner-key tool (``did-owner-key.py sign-change`` and ``revoke``), as on the
Get a DID-LLM page. The page only calls this server's API (api/owners.py): nothing changes
without the owners' signatures, and the page never asks for a private key or a recovery phrase
and never sends money. Only its own script and style run (hashes in the
Content-Security-Policy).
"""

from __future__ import annotations

import base64
import hashlib
import json
from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from dmd_gateway.api import brand
from dmd_gateway.api.get_did_page import APP_ROOT, OWNER_KEY_GUIDE
from dmd_gateway.api.get_did_page import PAGE_PATH as GET_DID_PATH
from dmd_gateway.api.hosts import is_app, to_app
from dmd_gateway.services.identity_status import REASON_LENGTH

router = APIRouter(include_in_schema=False)

PAGE_PATH = brand.MANAGE_PATH
_STATIC = files("dmd_gateway") / "static"
_STYLE = (
    brand.CSS
    + (_STATIC / "get-did.css").read_text(encoding="utf-8")
    + (_STATIC / "directory.css").read_text(encoding="utf-8")
    + (_STATIC / "manage.css").read_text(encoding="utf-8")
)
_SCRIPT = (_STATIC / "manage.js").read_text(encoding="utf-8")


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _headers() -> dict[str, str]:
    csp = (
        f"default-src 'none'; script-src 'sha256-{_sha256(_SCRIPT)}' "
        f"{brand.THEME_SCRIPT_SOURCE}; style-src 'sha256-{_sha256(_STYLE)}'; font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    return {
        "Cache-Control": f"no-cache, {brand.NO_TRANSFORM}",
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


BODY = """
<section class=hero><p class=eyebrow>For owners</p>
<h1>Manage your <span class=accent>DID-LLM</span></h1>
<p>Move your DID-LLM to a new wallet or key, add a second owner, or revoke it for good. Nothing
changes without its owners' signatures: a current owner approves, and each new owner accepts.</p>
</section>
<p class=safe><strong>Safe by design:</strong> this page never asks for a private key or a
recovery phrase, and never sends money. Signing sends nothing.</p>

<section class="card step current" id=s1>
<div class=step-head><span class=num>1</span><h2>Your DID-LLM</h2></div>
<div class=body>
<form id=find novalidate>
<label class=field for=did>The DID-LLM<small>It starts with did:web: (step 4 of Get a DID-LLM,
or your DID-LLM document).</small>
<input type=text id=did maxlength=300 autocomplete=off spellcheck=false
placeholder="did:web:api.debitmydata.io:agents:…"></label>
<div class=actions><button type=submit id=find-go>Find it</button></div>
</form>
<div id=found></div>
</div></section>

<section class="card step waiting" id=s2>
<div class=step-head><span class=num>2</span><h2>What to do</h2></div>
<div class=body>
<p class="small locked">Opens once your DID-LLM is found.</p>
<div id=choose hidden>
<fieldset id=action><legend>Choose</legend>
<label class=choice><input type=radio name=action value=change checked>
<strong>Change its owners</strong>
<small>A new wallet or key (a lost phone, a key that may have leaked, a new machine), a second
owner next to the first, or one owner less. Every earlier sign-in ends.</small></label>
<label class=choice id=revoke-choice><input type=radio name=action value=revoke>
<strong>Revoke it for good</strong>
<small>The DID-LLM, its credential and its entitlements stop working forever. It cannot be
undone, and it is not refunded.</small></label>
</fieldset>

<form id=change novalidate>
<p class=small>Name the owners it will have <strong>after</strong> the change. To keep one,
leave it as it is; to remove one, empty its field. A DID-LLM always keeps at least one owner.</p>
<label class=field for=new-wallet>Owner wallet after the change<small>An Ethereum-style wallet
(0x…, on Base unless it says otherwise). It needs no balance: it only signs.</small>
<input type=text id=new-wallet autocomplete=off spellcheck=false placeholder="0x…"></label>
<div class=wallets-holder id=use-wallet></div>
<label class=field for=new-key>Owner key after the change<small>An Ed25519 public key
(ed25519:z6Mk…). Make a new one with the
<a href="{guide}" target=_blank rel=noopener>owner-key tool</a>:
<code>python3 did-owner-key.py new ~/did-keys/new.key</code></small>
<input type=text id=new-key autocomplete=off spellcheck=false placeholder="ed25519:z6Mk…"></label>
<p class=error id=change-error hidden role=alert></p>
<div class=actions><button type=submit id=change-go>Prepare the change</button></div>
</form>

<form id=revoke novalidate hidden>
<div class=warning><p><strong>This cannot be undone.</strong> Once its owner signs, the
DID-LLM's document answers "gone", its credential shows as revoked, and its entitlements and
directory listing end, for good. Nobody can bring it back, DebitMyData included.</p></div>
<label class=field for=reason>Why (optional)<small>Kept with the revocation and public: no
secrets or personal data. {low} to {high} characters.</small>
<input type=text id=reason maxlength={high} autocomplete=off
placeholder="e.g. Agent retired"></label>
<fieldset id=revoke-signer hidden><legend>Sign with</legend>
<label class=choice><input type=radio name=revoke-signer value=eip155 checked><strong>The owner
wallet</strong> <span class="mono small" id=revoke-wallet></span></label>
<label class=choice><input type=radio name=revoke-signer value=ed25519><strong>The owner
key</strong> <span class="mono small" id=revoke-key></span></label>
</fieldset>
<label class=field for=confirm-word>Type REVOKE to confirm
<input type=text id=confirm-word autocomplete=off spellcheck=false placeholder="REVOKE"></label>
<p class=error id=revoke-error hidden role=alert></p>
<div class=actions><button type=submit id=revoke-go>Prepare the revocation</button></div>
</form>
</div>
</div></section>

<section class="card step waiting" id=s3>
<div class=step-head><span class=num>3</span><h2>Sign it</h2></div>
<div class=body><p class="small locked">Opens once the change or the revocation is
ready.</p></div></section>
<p id=announcer class=sr-only aria-live=polite></p>
<p class=fine>Owners can also do all of this from their own software: the owner-key tool, the
API (<code>/v1/dids/…/owner-changes</code>, <code>/v1/dids/…/revocations</code>) or the MCP
tools <code>debitmydata_change_owner</code> and <code>debitmydata_revoke_did</code>.</p>
"""


def page_html(request: Request) -> str:
    settings = request.app.state.settings
    staging = bool(settings.staging_test_tools)
    get_did = APP_ROOT if is_app(request) else GET_DID_PATH
    low, high = REASON_LENGTH
    body = BODY.format(guide=OWNER_KEY_GUIDE, low=low, high=high)
    config = json.dumps(
        {
            "ownerKeyGuide": OWNER_KEY_GUIDE,
            "getDid": get_did,
            "reasonLength": [low, high],
            # The gateway the owner-key tool must talk to (its default is production's).
            "apiBase": settings.public_base_url.rstrip("/"),
        }
    ).replace("<", "\\u003c")
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Manage your DID-LLM · DebitMyData</title>"
        '<meta name=description content="Change the owners of your DebitMyData DID-LLM, or '
        'revoke it, signed in your own wallet or with your own key.">'
        '<link rel=icon href="/favicon.png">'
        f"{brand.THEME_SCRIPT}<style>{_STYLE}</style></head><body>"
        f"{brand.header('manage', staging, get_did)}<main>{body}</main>"
        f"{brand.footer(staging, get_did)}"
        f'<script type="application/json" id="page-config">{config}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


@router.get(PAGE_PATH)
def manage_page(request: Request) -> Response:
    if moved := to_app(request):  # the app's address (hosts.py); the API stays
        return moved
    return HTMLResponse(page_html(request), headers=_headers())
