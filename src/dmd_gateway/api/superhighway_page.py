"""The page a web browser gets at the MCP server's address (/agenticsuperhighway, and /mcp).

The address is for AI agents: an MCP client talks to it with POST requests, and a GET asking
for the event stream opens the channel the server would push messages on. A browser's GET asks
for HTML instead; without this page it would get that empty stream, shown as a blank page that
never finishes loading. ``mcp_server`` serves this page to such requests only, so MCP clients
get exactly what they got before.

Like the policy pages, it has the look of the gateway's other pages (``brand``) and runs no
script but the light / dark theme one.

It also says what is sold here, as the Get a DID-LLM page does: the DID-LLM, its price word for
word ("$25.00 USD, paid once") and where to buy it, with the same structured data (schema.org
Product). Agent checkers that shop through the MCP server look for the price on the page at
this address.
"""

from __future__ import annotations

import base64
import hashlib
import json
from html import escape
from importlib.resources import files

from starlette.datastructures import Headers
from starlette.responses import HTMLResponse
from starlette.types import Scope

from dmd_gateway.api import brand, page_design
from dmd_gateway.api.get_did_page import (
    PRODUCT_NAME,
    checkout_url,
    mcp_url,
    page_url,
    price_text,
    structured_data,
)
from dmd_gateway.settings import Settings

_STYLE = brand.CSS + (files("dmd_gateway") / "static" / "policies.css").read_text(encoding="utf-8")
# The page's style in each design (page_design): its own, then the configured design's (the
# page answers at the MCP server's address, where no design is previewed).
STYLES = page_design.styles(_STYLE)


def _csp(style: str) -> str:
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    return (
        f"default-src 'none'; script-src {brand.THEME_SCRIPT_SOURCE}; "
        f"style-src 'sha256-{digest}'; img-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


CSPS = {design: _csp(style) for design, style in STYLES.items()}
CSP = CSPS[page_design.CLASSIC]
# Never stored: the same address answers MCP clients, so a cache must not hand them this page
# (Vary: Accept says the answer depends on what the request asks for).
HTML_HEADERS = {
    "Cache-Control": f"no-store, {brand.NO_TRANSFORM}",
    "Vary": "Accept",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def wants_page(scope: Scope) -> bool:
    """A browser opening the address: a GET (or HEAD) asking for HTML. MCP clients ask for
    ``text/event-stream`` (GET) or send POST requests, and never get the page."""
    if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
        return False
    accept = Headers(scope=scope).get("accept", "").lower()
    return "text/html" in accept and "text/event-stream" not in accept


def _structured_data(settings: Settings, price: tuple[str, str]) -> str:
    """The DID-LLM (Product, Offer) and DebitMyData, as on the Get a DID-LLM page."""
    product, organization, _website = structured_data(settings, price)
    data = json.dumps([product, organization], separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{data}</script>'


def page_html(settings: Settings, price: tuple[str, str] = ("25.00", "USD")) -> str:
    staging = bool(settings.staging_test_tools)
    address = escape(mcp_url(settings))
    sold = (
        "<h2>Sold here</h2>"
        f"<p><strong>{PRODUCT_NAME}</strong>: <span class=price>{escape(price_text(price))}"
        "</span>. A verifiable identity for an AI agent, a bot, a person or an organization, "
        "with a credential signed by DebitMyData. No account, no password.</p>"
        f'<p><a href="{escape(checkout_url(settings))}">Buy a DID-LLM</a> (people) · '
        f'<a href="{escape(page_url(settings))}">its page</a> · agents: the tool '
        "<code>debitmydata_open_checkout</code></p>"
    )
    note = (
        "<p class=note><strong>Test server.</strong> Payments here use test networks whose "
        "coins have no value, and DID-LLMs issued here are test DID-LLMs.</p>"
        if settings.environment != "production"
        else ""
    )
    body = (
        "<h1>Agentic Superhighway</h1>"
        "<p>This address is DebitMyData's MCP server for AI agents. An agent connects to it "
        "with the Model Context Protocol to get a DID-LLM, DebitMyData's identity for AI "
        "agents, bots, people and organizations, and to pay for it by itself. There is "
        "nothing more to see here in a web browser.</p>"
        f"{note}"
        f"{sold}"
        "<h2>Connect an agent</h2>"
        f"<p>In the agent's MCP client, add a remote MCP server (streamable HTTP) with this "
        f"address:</p><p><code>{address}</code></p>"
        f"<p>Its first address, <code>{escape(brand.MCP_OLD_PATHS[0])}</code>, still works.</p>"
        "<p>Without MCP, the same steps are in "
        f'<a href="{brand.LLMS_PATH}">{brand.LLMS_PATH}</a> (instructions for agents, in plain '
        'text) and the REST API, starting with <a href="/v1/discovery">/v1/discovery</a>.</p>'
        "<h2>For people</h2>"
        f'<p><a href="{brand.GET_DID_PATH}">Get a DID-LLM</a> for an agent, a bot, yourself or '
        f'your organization, and read the <a href="{brand.POLICIES_PATH}">policies</a>.</p>'
    )
    design = page_design.configured(settings)
    return (
        f"<!doctype html><html lang=en{page_design.root_attribute(design)}><head>"
        '<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Agentic Superhighway · DebitMyData Agent Gateway</title>"
        f'<link rel=icon href="/favicon.png">{page_design.preload(design)}'
        f"{_structured_data(settings, price)}"
        f"{brand.THEME_SCRIPT}<style>{STYLES[design]}</style></head><body>"
        f"{brand.header('', staging)}"
        f"<main><article>{body}</article></main>{brand.footer(staging)}"
        "</body></html>"
    )


def page(settings: Settings, price: tuple[str, str] = ("25.00", "USD")) -> HTMLResponse:
    design = page_design.configured(settings)
    headers = {**HTML_HEADERS, "Content-Security-Policy": CSPS[design]}
    return HTMLResponse(page_html(settings, price), headers=headers)
