"""The directory's pages, in the debitmydata.com look:

    /directory              the listings, newest first, with a search (no script needed)
    /directory/{ULID}       one listing, to link to
    /directory/list         for owners: list a DID-LLM, change its listing or remove it

The first two are made on the server from the same data as ``GET /v1/directory``; every value
is escaped. The listing page runs its own script (``directory-list.js``), which only calls
this server: the owner signs in their own wallet, or with their key in their own tool, as on
the Get a DID-LLM page. No page asks for a private key or a recovery phrase.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from importlib.resources import files
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from dmd_gateway.api import brand
from dmd_gateway.api.directory import LIST_PATH, PAGE_PATH
from dmd_gateway.api.get_did_page import APP_ROOT, is_app
from dmd_gateway.api.get_did_page import PAGE_PATH as GET_DID_PATH
from dmd_gateway.db.models import AgentIdentity, DirectoryListing
from dmd_gateway.db.session import transaction
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.did import did_document_url, did_suffix
from dmd_gateway.services import directory
from dmd_gateway.services.registrations import identity_record_url

router = APIRouter(include_in_schema=False)

ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_STATIC = files("dmd_gateway") / "static"
_STYLE = (
    brand.CSS
    + (_STATIC / "get-did.css").read_text(encoding="utf-8")
    + (_STATIC / "directory.css").read_text(encoding="utf-8")
)
_SCRIPT = (_STATIC / "directory-list.js").read_text(encoding="utf-8")
OWNER_KEY_GUIDE = "https://github.com/DebitMyData-PoW/agent-gateway/blob/main/docs/owner-key.md"
TYPES = (("", "All types"), *((key, label) for key, label in directory.ENTITY_LABELS.items()))


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _csp(script: str | None) -> str:
    scripts = f"'sha256-{_sha256(script)}' " if script else ""
    return (
        f"default-src 'none'; script-src {scripts}{brand.THEME_SCRIPT_SOURCE}; "
        f"style-src 'sha256-{_sha256(_STYLE)}'; font-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


def _headers(script: str | None) -> dict[str, str]:
    return {
        "Cache-Control": f"no-cache, {brand.NO_TRANSFORM}",
        "Content-Security-Policy": _csp(script),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _get_did(request: Request) -> str:
    return APP_ROOT if is_app(request) else GET_DID_PATH


def _page(request: Request, title: str, body: str, script: str | None = None) -> str:
    staging = bool(request.app.state.settings.staging_test_tools)
    get_did = _get_did(request)
    tail = ""
    if script:
        config = json.dumps(
            {"ownerKeyGuide": OWNER_KEY_GUIDE, "getDid": get_did, "terms": "/policies/terms"}
        ).replace("<", "\\u003c")
        tail = (
            f'<script type="application/json" id="page-config">{config}</script>'
            f"<script>{script}</script>"
        )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)} · DebitMyData</title>"
        '<meta name=description content="The DebitMyData DID-LLM directory: AI agents, bots and '
        'people with a DID-LLM, listed by their owners.">'
        '<link rel=icon href="/favicon.png">'
        f"{brand.THEME_SCRIPT}<style>{_STYLE}</style></head><body>"
        f"{brand.header('directory', staging, get_did)}<main>{body}</main>"
        f"{brand.footer(staging, get_did)}{tail}</body></html>"
    )


def _report_link(identity: AgentIdentity) -> str:
    query = urlencode(
        {
            "subject": f"Directory listing report: {identity.display_name}",
            "body": f"DID-LLM: {identity.public_identifier}\nWhat is wrong with it:\n",
        },
        quote_via=quote,
    )
    return f"mailto:{brand.CONTACT}?{query}"


def _card(
    base: str, identity: AgentIdentity, listing: DirectoryListing, *, one: bool = False
) -> str:
    suffix = did_suffix(identity.id)
    heading = "h2" if one else "h3"
    name = _e(identity.display_name)
    title = name if one else f'<a href="{PAGE_PATH}/{suffix}">{name}</a>'
    facts = []
    if listing.owner_name:
        facts.append(("Owner", listing.owner_name))
    where = ", ".join(part for part in (listing.region, listing.country) if part)
    if where:
        facts.append(("Where", where))
    if listing.created_on:
        facts.append(("Created", listing.created_on.isoformat()))
    if listing.listed_at:
        facts.append(("Listed", listing.listed_at.date().isoformat()))
    rows = "".join(f"<dt>{_e(label)}</dt><dd>{_e(value)}</dd>" for label, value in facts)
    links = [
        f'<a href="{_e(did_document_url(base, identity.id))}" target=_blank rel=noopener>'
        "DID-LLM document</a>",
        f'<a href="{_e(identity_record_url(base, identity.id))}" target=_blank rel=noopener>'
        "Identity record</a>",
    ]
    if one:
        links.append(
            f'<a href="/v1/directory/{_e(identity.id)}" target=_blank rel=noopener>JSON</a>'
        )
    links.append(f'<a href="{_e(_report_link(identity))}">Report</a>')
    kind = directory.ENTITY_LABELS.get(listing.entity_type or "", "")
    return (
        f'<li class="entry{" one" if one else ""}" id="{suffix}">'
        f"<p class=top><span class=kind>{_e(kind)}</span>"
        '<span class=verified title="Listed by the DID-LLM\'s owner, with a signature">'
        "✓ Signed by its owner</span></p>"
        f"<{heading}>{title}</{heading}>"
        f"<p class=specialty>{_e(listing.specialty)}</p>"
        f"<p class=description>{_e(listing.description)}</p>"
        f"{f'<dl>{rows}</dl>' if rows else ''}"
        f"<p class=did>{_e(identity.public_identifier)}</p>"
        f"<p class=links>{''.join(links)}</p></li>"
    )


FINE_PRINT = (
    "<p class=fine>Each listing was published by the DID-LLM's owner, who signed it with the "
    "DID-LLM's own key or wallet. DebitMyData checks that signature, not what the listing "
    f'says. Something wrong with a listing? <a href="mailto:{brand.CONTACT}">Tell us</a>. '
    'Listings follow the <a href="/policies/terms">terms</a> and the '
    '<a href="/policies/privacy">privacy policy</a>.</p>'
)


@router.get(PAGE_PATH)
def directory_page(
    request: Request,
    q: str | None = Query(default=None, max_length=directory.MAX_SEARCH),
    type: str | None = Query(default=None, max_length=20),  # noqa: A002
    cursor: str | None = Query(default=None, max_length=200),
) -> HTMLResponse:
    base = request.app.state.settings.public_base_url.rstrip("/")
    entity_type = type if type in directory.ENTITY_LABELS else None
    text = " ".join((q or "").split())
    try:
        with transaction(request.app.state.session_factory) as session:
            page = directory.search(
                session, text=text, entity_type=entity_type, cursor=cursor or None
            )
            total = directory.count_shown(session)
            cards = "".join(_card(base, identity, listing) for identity, listing in page.entries)
    except ApiError:  # a cursor that is not one of ours: the first page
        return directory_page(request, q=q, type=type, cursor=None)
    options = "".join(
        f'<option value="{key}"{" selected" if key == (entity_type or "") else ""}>{label}</option>'
        for key, label in TYPES
    )
    searching = bool(text or entity_type)
    if searching:
        found = (
            f"<p class=found>{len(page.entries)}{'+' if page.next_cursor else ''} "
            f"listing{'s' if len(page.entries) != 1 else ''} found. "
            f'<a href="{PAGE_PATH}">Show all {total}</a></p>'
        )
    else:
        found = f"<p class=found>{total} listing{'s' if total != 1 else ''}.</p>"
    if not page.entries:
        cards_html = (
            "<p class=empty>No listing matches. Try other words, or "
            f'<a href="{PAGE_PATH}">show them all</a>.</p>'
            if searching
            else "<p class=empty>No DID-LLM is listed yet. Have one? "
            f'<a href="{LIST_PATH}">Be the first</a>.</p>'
        )
    else:
        cards_html = f"<ul class=entries>{cards}</ul>"
    more = ""
    if page.next_cursor:
        params = {"q": text, "type": entity_type or "", "cursor": page.next_cursor}
        query = urlencode({key: value for key, value in params.items() if value})
        more = f'<p class=more><a class="button secondary" href="{PAGE_PATH}?{query}">More</a></p>'
    body = (
        "<section class=hero><p class=eyebrow>DID-LLM directory</p>"
        "<h1>The <span class=accent>DebitMyData</span> directory</h1>"
        "<p>AI agents, bots, avatars and the people behind them, each with a DID-LLM signed by "
        "DebitMyData. Every listing was chosen and signed by the DID-LLM's owner.</p>"
        f'<p class=cta><a class="button go" href="{LIST_PATH}">List my DID-LLM</a>'
        f'<a class="button ghost" href="{_get_did(request)}">Get a DID-LLM</a></p></section>'
        f'<form class=search method=get action="{PAGE_PATH}" role=search>'
        "<label for=q>Search<input type=text id=q name=q maxlength=80 "
        f'value="{_e(text)}" placeholder="Name, specialty, country…"></label>'
        f"<label for=type>Type<select id=type name=type>{options}</select></label>"
        "<button type=submit>Search</button></form>"
        f"{found}{cards_html}{more}{FINE_PRINT}"
    )
    return HTMLResponse(_page(request, "DID-LLM directory", body), headers=_headers(None))


@router.get(LIST_PATH)
def list_page(request: Request) -> HTMLResponse:
    options = "".join(
        f"<option value={key}>{label}</option>" for key, label in directory.ENTITY_LABELS.items()
    )
    words = directory.DESCRIPTION_WORDS
    body = f"""
<section class=hero><p class=eyebrow>DID-LLM directory</p>
<h1>List your <span class=accent>DID-LLM</span></h1>
<p>Put your DID-LLM in the <a href="{PAGE_PATH}">directory</a>, so that people and agents can find
it. Only its owner can: you sign the listing with the DID-LLM's own wallet or key, as when it was
issued. Change it or remove it the same way, at any time.</p></section>
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
<div class=step-head><span class=num>2</span><h2>Its public profile</h2></div>
<div class=body>
<p class="small locked">Opens once your DID-LLM is found.</p>
<form id=profile novalidate hidden>
<p class=small>Everything here is public, next to the DID-LLM's name. Plain text only: no links,
no e-mail addresses.</p>
<label class=field for=entity_type>Type<select id=entity_type required>
<option value="" selected disabled>Choose a type</option>{options}</select></label>
<label class=field for=specialty>Specialty / capabilities<small>What it does, in a few words.
</small><input type=text id=specialty maxlength=120 placeholder="e.g. Customer support"></label>
<label class=field for=description>Description <span class=count id=words></span>
<small>What it is and what it is for: {words} words at most.</small>
<textarea id=description class=prose rows=4 maxlength=700></textarea></label>
<div class=two>
<label class=field for=owner_name>Owner's name<small>Optional, public.</small>
<input type=text id=owner_name maxlength=80></label>
<label class=field for=created_on>Date created<small>Optional: when the entity was created.
</small><input type=date id=created_on></label>
<label class=field for=region>State / region<small>Optional.</small>
<input type=text id=region maxlength=60 placeholder="e.g. California"></label>
<label class=field for=country>Country<small>Optional.</small>
<input type=text id=country maxlength=60 placeholder="e.g. USA"></label>
</div>
<fieldset id=signer hidden><legend>Sign with</legend>
<label class=choice><input type=radio name=signer value=eip155 checked><strong>The owner
wallet</strong> <span class="mono small" id=signer-wallet></span></label>
<label class=choice><input type=radio name=signer value=ed25519><strong>The owner key</strong>
<span class="mono small" id=signer-key></span></label>
</fieldset>
<label class=check><input type=checkbox id=agree><span>I own this DID-LLM, and I agree that this
profile is published, under the <a href="/policies/terms" target=_blank rel=noopener>terms</a>
and the <a href="/policies/privacy" target=_blank rel=noopener>privacy policy</a>.</span></label>
<p class=error id=profile-error hidden role=alert></p>
<div class=actions><button type=submit id=publish>Sign and publish</button>
<button type=button class=secondary id=remove hidden>Remove it from the directory</button></div>
</form>
</div></section>

<section class="card step waiting" id=s3>
<div class=step-head><span class=num>3</span><h2>Sign it</h2></div>
<div class=body><p class="small locked">Opens once the profile is ready.</p></div></section>
<p id=announcer class=sr-only aria-live=polite></p>
{FINE_PRINT}
"""  # noqa: S608 (an HTML form: its "select" is not SQL)
    return HTMLResponse(
        _page(request, "List your DID-LLM", body, _SCRIPT), headers=_headers(_SCRIPT)
    )


@router.get(PAGE_PATH + "/{suffix}")
def entry_page(request: Request, suffix: str) -> HTMLResponse:
    base = request.app.state.settings.public_base_url.rstrip("/")
    with transaction(request.app.state.session_factory) as session:
        found = None
        if ULID.fullmatch(suffix):
            found = directory.shown_entry(session, f"dmd_identity_{suffix}")
        if found is None:
            body = (
                "<section class=hero><p class=eyebrow>DID-LLM directory</p>"
                "<h1>Not in the <span class=accent>directory</span></h1>"
                "<p>This DID-LLM is not listed: it never was, its owner removed it, or it is "
                f'no longer active.</p><p class=cta><a class="button go" href="{PAGE_PATH}">'
                "See the directory</a></p></section>"
            )
            return HTMLResponse(
                _page(request, "Not in the directory", body),
                status_code=404,
                headers=_headers(None),
            )
        identity, listing = found
        card = _card(base, identity, listing, one=True)
        title = identity.display_name
    body = (
        f'<p><a href="{PAGE_PATH}">← The directory</a></p><ul class="entries single">{card}</ul>'
        f"{FINE_PRINT}"
    )
    return HTMLResponse(_page(request, title, body), headers=_headers(None))
