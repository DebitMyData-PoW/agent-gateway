"""The policy pages, served by the gateway (DEC-09).

    GET /policies                 the list of policies (HTML)
    GET /policies/{name}          one policy (HTML), e.g. /policies/refunds
    GET /policies/{name}.md       the same text as Markdown, for agents
    GET /v1/policies              the list as JSON: version, status and SHA-256 of each text

The texts and how they are filled in are in ``dmd_gateway.services.policy_pages``. The pages
have the debitmydata.com look of the gateway's other pages (``brand``) and run no script.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import date
from html import escape
from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from dmd_gateway.api import brand
from dmd_gateway.errors import ApiError
from dmd_gateway.services.policy_pages import PolicyPage

router = APIRouter()

# no-transform: proxies must serve the pages as they are. It also keeps Cloudflare from
# hiding email addresses behind a script, which these pages (only their own script allowed)
# cannot run.
CACHE = "public, max-age=300, no-transform"

_STYLE = brand.CSS + (files("dmd_gateway") / "static" / "policies.css").read_text(encoding="utf-8")
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
# Nothing but the page's own style, the gateway's icon and font may load, and no script but the
# small light / dark theme one every page has (brand.THEME_SCRIPT).
CSP = (
    f"default-src 'none'; script-src {brand.THEME_SCRIPT_SOURCE}; "
    f"style-src 'sha256-{_STYLE_HASH}'; img-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HTML_HEADERS = {
    "Cache-Control": f"{CACHE}, {brand.NO_TRANSFORM}",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _pages(request: Request) -> dict[str, PolicyPage]:
    return request.app.state.policy_pages


def _notices(request: Request, page: PolicyPage | None) -> str:
    notes = []
    if request.app.state.settings.environment != "production":
        notes.append(
            "<p class=note><strong>Test server.</strong> Payments here use test networks "
            "whose coins have no value, and DIDs issued here are test DIDs.</p>"
        )
    if page is not None and page.status == "draft":
        notes.append(
            "<p class=note><strong>Draft.</strong> This text is awaiting DebitMyData's "
            "approval and may still change.</p>"
        )
    return "".join(notes)


def _document(request: Request, title: str, current: str | None, body: str) -> str:
    links = "".join(
        f'<a href="{page.path}"{" aria-current=page" if page.name == current else ""}>'
        f"{escape(page.title)}</a>"
        for page in _pages(request).values()
    )
    staging = bool(request.app.state.settings.staging_test_tools)
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)} · DebitMyData Agent Gateway</title>"
        '<link rel=icon href="/favicon.png">'
        f"{brand.THEME_SCRIPT}<style>{_STYLE}</style></head><body>"
        f"{brand.header('policies', staging)}"
        f'<main><nav class=policies aria-label="Policies">{links}</nav>'
        f"<article>{body}</article></main>{brand.footer(staging)}"
        "</body></html>"
    )


def _version_line(page: PolicyPage) -> str:
    return (
        f"<p class=meta>Version {page.version.isoformat()}"
        f"{' (draft)' if page.status == 'draft' else ''}"
        f' · <a href="{page.path}.md">Markdown</a></p>'
    )


@router.get("/policies", include_in_schema=False)
def policy_index(request: Request) -> HTMLResponse:
    items = "".join(
        f'<li><a href="{page.path}"><strong>{escape(page.title)}</strong></a>'
        f"<br>{escape(page.summary)}{_version_line(page)}</li>"
        for page in _pages(request).values()
    )
    website = (
        "<p class=meta>DebitMyData's websites and apps have their own "
        '<a href="https://app.debitmydata.com/terms-of-services">terms of service</a> and '
        '<a href="https://app.debitmydata.com/privacy-policy">privacy policy</a>.</p>'
    )
    body = (
        f"{_notices(request, None)}<h1>Policies</h1>"
        "<p>These policies apply to the DebitMyData Agent Gateway and the DIDs it issues.</p>"
        f"<ul class=pages>{items}</ul>{website}"
    )
    return HTMLResponse(_document(request, "Policies", None, body), headers=HTML_HEADERS)


@router.get("/policies/{name}", include_in_schema=False)
def policy_page(name: str, request: Request) -> Response:
    markdown = name.endswith(".md")
    page = _pages(request).get(name.removesuffix(".md"))
    if page is None:
        raise ApiError(404, "not_found", "Unknown policy; see /policies for the list.")
    if markdown:
        return Response(
            page.markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Cache-Control": CACHE, "X-Content-Type-Options": "nosniff"},
        )
    # The text's own title is the page's heading, so the version line goes after it.
    heading, _, rest = page.html.partition("</h1>")
    body = f"{_notices(request, page)}{heading}</h1>{_version_line(page)}{rest}"
    return HTMLResponse(_document(request, page.title, page.name, body), headers=HTML_HEADERS)


class PolicySummary(BaseModel):
    name: str = Field(description="e.g. refunds")
    title: str
    version: date = Field(description="The date of this version of the text.")
    status: str = Field(description="draft (awaiting approval) or approved")
    summary: str
    url: str
    markdown_url: str
    sha256: str = Field(description="SHA-256 of the Markdown text, to tell versions apart.")


class PolicyList(BaseModel):
    terms_version: str = Field(description="The terms version that new quotes name.")
    policies: list[PolicySummary]


@router.get(
    "/v1/policies",
    response_model=PolicyList,
    tags=["discovery"],
    summary="The policies that apply to registrations: terms, privacy, payments, refunds, DIDs",
)
def policy_list(request: Request, response: Response) -> PolicyList:
    response.headers["Cache-Control"] = CACHE
    base = request.app.state.settings.public_base_url.rstrip("/")
    terms = [s.terms_version for s in request.app.state.registry.services.values()]
    return PolicyList(
        terms_version=terms[0] if terms else "",
        policies=[
            PolicySummary(
                name=page.name,
                title=page.title,
                version=page.version,
                status=page.status,
                summary=page.summary,
                url=f"{base}{page.path}",
                markdown_url=f"{base}{page.path}.md",
                sha256=page.sha256,
            )
            for page in _pages(request).values()
        ],
    )
