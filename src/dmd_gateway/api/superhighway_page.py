"""The page a web browser gets at the MCP server's address (/agenticsuperhighway, and /mcp).

The address is for AI agents: an MCP client talks to it with POST requests, and a GET asking
for the event stream opens the channel the server would push messages on. A browser's GET asks
for HTML instead; without this page it would get that empty stream, shown as a blank page that
never finishes loading. ``mcp_server`` serves this page to such requests only, so MCP clients
get exactly what they got before.

Like the policy pages, it has the look of the gateway's other pages (``brand``) and runs no
script but the light / dark theme one.
"""

from __future__ import annotations

import base64
import hashlib
from html import escape
from importlib.resources import files

from starlette.datastructures import Headers
from starlette.responses import HTMLResponse
from starlette.types import Scope

from dmd_gateway.api import brand
from dmd_gateway.api.get_did_page import mcp_url
from dmd_gateway.settings import Settings

_STYLE = brand.CSS + (files("dmd_gateway") / "static" / "policies.css").read_text(encoding="utf-8")
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
CSP = (
    f"default-src 'none'; script-src {brand.THEME_SCRIPT_SOURCE}; "
    f"style-src 'sha256-{_STYLE_HASH}'; img-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
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


def page_html(settings: Settings) -> str:
    staging = bool(settings.staging_test_tools)
    address = escape(mcp_url(settings))
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
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Agentic Superhighway · DebitMyData Agent Gateway</title>"
        '<link rel=icon href="/favicon.png">'
        f"{brand.THEME_SCRIPT}<style>{_STYLE}</style></head><body>"
        f"{brand.header('', staging)}"
        f"<main><article>{body}</article></main>{brand.footer(staging)}"
        "</body></html>"
    )


def page(settings: Settings) -> HTMLResponse:
    return HTMLResponse(page_html(settings), headers=HTML_HEADERS)
