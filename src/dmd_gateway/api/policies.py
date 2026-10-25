"""The policy pages, served by the gateway (DEC-09).

    GET /policies                 the list of policies (HTML)
    GET /policies/{name}          one policy (HTML), e.g. /policies/refunds
    GET /policies/{name}.md       the same text as Markdown, for agents
    GET /v1/policies              the list as JSON: version, status and SHA-256 of each text

The texts and how they are filled in are in ``dmd_gateway.services.policy_pages``.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import date
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from dmd_gateway.errors import ApiError
from dmd_gateway.services.policy_pages import PolicyPage

router = APIRouter()

CACHE = "public, max-age=300"

_STYLE = """
:root{--bg:#fff;--fg:#1d2330;--muted:#5b6475;--line:#e3e6ec;--accent:#0b5cad;--note:#fff5d6;
--note-line:#e8c55a;--code:#f3f5f8}
@media (prefers-color-scheme:dark){:root{--bg:#11151c;--fg:#e6e9ef;--muted:#9aa3b2;
--line:#2a313d;--accent:#7ab8ff;--note:#3a3216;--note-line:#8a7428;--code:#1b212b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
header,main,footer{max-width:46rem;margin:0 auto;padding:0 16px}
header{padding-top:24px;border-bottom:1px solid var(--line)}
header a.home{display:flex;gap:10px;align-items:center;color:var(--fg);text-decoration:none;
font-weight:600}
header img{width:28px;height:28px}
nav{display:flex;flex-wrap:wrap;gap:4px 16px;padding:12px 0;font-size:.95rem}
nav a{color:var(--muted);text-decoration:none}
nav a[aria-current]{color:var(--fg);font-weight:600}
a{color:var(--accent)}
.note{margin:20px 0 0;padding:10px 14px;background:var(--note);
border-left:4px solid var(--note-line);border-radius:4px}
.meta{color:var(--muted);font-size:.9rem;margin:0}
h1{font-size:1.8rem;line-height:1.25;margin:28px 0 4px}
h2{font-size:1.25rem;margin:32px 0 8px}
h3{font-size:1.05rem;margin:24px 0 6px}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.88em;
overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:.95rem;display:block;
overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
th{background:var(--code)}
ul.pages{list-style:none;padding:0}
ul.pages li{padding:12px 0;border-bottom:1px solid var(--line)}
footer{margin-top:40px;padding-top:12px;padding-bottom:32px;border-top:1px solid var(--line);
color:var(--muted);font-size:.9rem}
""".strip()
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
# Nothing but the page's own style and the gateway's icon may load; no scripts at all.
CSP = (
    f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; img-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HTML_HEADERS = {
    "Cache-Control": CACHE,
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
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)} · DebitMyData Agent Gateway</title>"
        '<link rel=icon href="/favicon.png">'
        f"<style>{_STYLE}</style></head><body>"
        '<header><a class=home href="/policies"><img src="/favicon.png" alt="">'
        "DebitMyData Agent Gateway</a>"
        f"<nav>{links}</nav></header>"
        f"<main>{body}</main>"
        '<footer>Questions: <a href="mailto:info@debitmydata.com">info@debitmydata.com</a>'
        ' · <a href="/docs">API docs</a></footer>'
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
    body = f"{_notices(request, None)}<h1>Policies</h1><ul class=pages>{items}</ul>"
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
