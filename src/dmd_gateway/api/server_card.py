"""How MCP clients, crawlers and registries find the Agentic Superhighway by themselves.

- The Server Card (MCP Server Cards, SEP-2127, an MCP extension): a small public JSON document
  saying what the MCP server is and how to connect to it, read before connecting. It is at the
  address the extension recommends, the MCP address followed by ``/server-card``:
  ``/agenticsuperhighway/server-card`` (and ``/mcp/server-card``, for the first address). It
  lists no tools: an MCP client asks the server for them (``tools/list``), as the extension
  requires.
- The AI Catalog, ``/.well-known/ai-catalog.json``: the site's list of AI services, which
  points to the card. Crawlers that only know the domain start there.
- The two files of DebitMyData's discovery runbook (the "AgenticSuperhighway Discovery &
  Publishing SOP"), written for earlier drafts of MCP discovery (SEP-1649, SEP-1960) before
  SEP-2127 replaced them: ``/.well-known/mcp/server-card.json``, the card in the runbook's
  format (``mcp_version``, ``server_info``, ``endpoints``, ``homepage``) with the tools the
  MCP server lists right now, and ``/.well-known/mcp``, a short manifest (``mcp_version`` and
  ``endpoints``). Both are ``application/json`` and point to the standard card.

The card carries the same name, description and address as ``server.json`` at the repository
root, which lists the server in the official MCP Registry (registry.modelcontextprotocol.io);
``registry_entry`` builds that file's content and a test keeps the two the same.

All of it is public, read-only metadata: open to every origin (CORS), with an ETag so that
clients can ask whether it changed, answered to GET and HEAD. The card is cached for an hour,
the files under ``/.well-known/`` for five minutes (the runbook's launch setting).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSIONS,
)

from dmd_gateway import __version__
from dmd_gateway.api import brand
from dmd_gateway.api.get_did_page import ICON_VERSION, mcp_url, page_url
from dmd_gateway.api.oauth import sign_in_open
from dmd_gateway.settings import Settings

router = APIRouter(include_in_schema=False)

CARD_SCHEMA = "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
REGISTRY_SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
CARD_TYPE = "application/mcp-server-card+json"
CATALOG_TYPE = "application/ai-catalog+json"
JSON_TYPE = "application/json"
CARD_SUFFIX = "/server-card"
CATALOG_PATH = "/.well-known/ai-catalog.json"
# The runbook's two files (SEP-1649 and SEP-1960 drafts).
RUNBOOK_CARD_PATH = "/.well-known/mcp/server-card.json"
RUNBOOK_MANIFEST_PATH = "/.well-known/mcp"

# The server's name in the MCP Registry: the namespace is debitmydata.io's, reversed (the
# Registry checks it with a DNS record on debitmydata.io, docs/production.md section 13).
DOMAIN = "debitmydata.io"
NAMESPACE = "io.debitmydata"
SERVER = "agenticsuperhighway"

TITLE = "DebitMyData Agentic Superhighway"
# At most 100 characters (the card's and the Registry's limit).
DESCRIPTION = (
    "Get a DID-LLM, DebitMyData's verifiable identity for AI agents, and pay for it by yourself."
)
TEST_DESCRIPTION = "Test server of the Agentic Superhighway: test networks and test DID-LLMs only."

CACHE = "public, max-age=3600"
WELL_KNOWN_CACHE = "public, max-age=300"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET",
    "Access-Control-Allow-Headers": "Content-Type, If-None-Match",
    "Access-Control-Expose-Headers": "ETag",
}


def server_name(settings: Settings) -> str:
    """``io.debitmydata/agenticsuperhighway`` in production; staging and development add
    their name, so a test server is never taken for the real one."""
    if settings.environment == "production":
        return f"{NAMESPACE}/{SERVER}"
    return f"{NAMESPACE}/{SERVER}-{settings.environment}"


def card_url(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}{brand.MCP_PATH}{CARD_SUFFIX}"


def _icons(settings: Settings) -> list[dict[str, Any]]:
    base = settings.public_base_url.rstrip("/")
    icons: list[dict[str, Any]] = [
        {"src": f"{base}/favicon.png", "mimeType": "image/png", "sizes": ["64x64"]}
    ]
    if settings.app_host:  # the app's icons are served on its own address only
        icons.append(
            {
                "src": f"https://{settings.app_host}/app/icon-512.png?v={ICON_VERSION}",
                "mimeType": "image/png",
                "sizes": ["512x512"],
            }
        )
    return icons


def _identity(settings: Settings) -> dict[str, Any]:
    production = settings.environment == "production"
    return {
        "name": server_name(settings),
        "title": TITLE if production else f"{TITLE} ({settings.environment})",
        "description": DESCRIPTION if production else TEST_DESCRIPTION,
        "version": __version__,
        "websiteUrl": page_url(settings),
        "repository": {"url": brand.SOURCE_CODE, "source": "github"},
        "icons": _icons(settings),
    }


# What this server's MCP library speaks: the handshake versions and the newer ones.
PROTOCOL_VERSIONS = [*HANDSHAKE_PROTOCOL_VERSIONS, *MODERN_PROTOCOL_VERSIONS]


def server_card(settings: Settings) -> dict[str, Any]:
    remote = {
        "type": "streamable-http",
        "url": mcp_url(settings),
        "supportedProtocolVersions": PROTOCOL_VERSIONS,
    }
    return {"$schema": CARD_SCHEMA, **_identity(settings), "remotes": [remote]}


def registry_entry(settings: Settings) -> dict[str, Any]:
    """The content of server.json, for the MCP Registry (production settings)."""
    remote = {"type": "streamable-http", "url": mcp_url(settings)}
    return {"$schema": REGISTRY_SCHEMA, **_identity(settings), "remotes": [remote]}


def ai_catalog(settings: Settings) -> dict[str, Any]:
    local_name = server_name(settings).split("/", 1)[1]
    return {
        "specVersion": "1.0",
        "entries": [
            {
                "identifier": f"urn:air:{DOMAIN}:mcp:{local_name}",
                "type": CARD_TYPE,
                "url": card_url(settings),
            }
        ],
    }


def _endpoint(settings: Settings, sign_in: bool) -> dict[str, Any]:
    # Registering needs no authentication (agents pay for each DID-LLM and prove control of
    # their own key). Once agents can sign in, OAuth 2.1 is offered, never required.
    return {
        "url": mcp_url(settings),
        "transport": "streamable-http",
        "protocol_versions": PROTOCOL_VERSIONS,
        "authentication": ["oauth2.1"] if sign_in else [],
    }


def runbook_manifest(settings: Settings, *, sign_in: bool = False) -> dict[str, Any]:
    """``/.well-known/mcp``: the runbook's short manifest."""
    identity = _identity(settings)
    return {
        "mcp_version": LATEST_PROTOCOL_VERSION,
        "server_name": identity["title"],
        "server_version": __version__,
        "endpoints": [_endpoint(settings, sign_in)],
        "server_card": card_url(settings),
        "documentation": f"{settings.public_base_url.rstrip('/')}{brand.LLMS_PATH}",
    }


def runbook_card(
    settings: Settings, tools: list[dict[str, Any]], *, sign_in: bool = False
) -> dict[str, Any]:
    """``/.well-known/mcp/server-card.json``: the card in the runbook's format, with the tools
    the MCP server lists (``tools``: name, title and description of each)."""
    identity = _identity(settings)
    return {
        "mcp_version": LATEST_PROTOCOL_VERSION,
        "server_info": {
            "name": identity["title"],
            "version": __version__,
            "description": identity["description"],
        },
        "endpoints": [_endpoint(settings, sign_in)],
        "homepage": identity["websiteUrl"],
        "documentation": f"{settings.public_base_url.rstrip('/')}{brand.LLMS_PATH}",
        "server_card": card_url(settings),
        "tools": tools,
    }


async def _tools(request: Request) -> list[dict[str, Any]]:
    tools = await request.app.state.mcp.list_tools()
    return [
        {"name": tool.name, "title": tool.title, "description": tool.description} for tool in tools
    ]


def _not_modified(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    return "*" in tags or etag in tags


def _document(
    request: Request, document: dict[str, Any], media_type: str, cache: str = CACHE
) -> Response:
    body = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    headers = {**CORS, "Cache-Control": cache, "ETag": etag, "X-Content-Type-Options": "nosniff"}
    if _not_modified(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=media_type, headers=headers)


def _preflight() -> Response:
    # A page on another site that sends If-None-Match asks first (CORS preflight).
    return Response(status_code=204, headers={**CORS, "Access-Control-Max-Age": "86400"})


def card(request: Request) -> Response:
    return _document(request, server_card(request.app.state.settings), CARD_TYPE)


def catalog(request: Request) -> Response:
    settings = request.app.state.settings
    return _document(request, ai_catalog(settings), CATALOG_TYPE, WELL_KNOWN_CACHE)


async def runbook_card_file(request: Request) -> Response:
    settings = request.app.state.settings
    document = runbook_card(
        settings, await _tools(request), sign_in=sign_in_open(request.app.state)
    )
    return _document(request, document, JSON_TYPE, WELL_KNOWN_CACHE)


def runbook_manifest_file(request: Request) -> Response:
    settings = request.app.state.settings
    document = runbook_manifest(settings, sign_in=sign_in_open(request.app.state))
    return _document(request, document, JSON_TYPE, WELL_KNOWN_CACHE)


CARD_PATHS = tuple(f"{path}{CARD_SUFFIX}" for path in (brand.MCP_PATH, *brand.MCP_OLD_PATHS))
ROUTES = [
    *((path, card) for path in CARD_PATHS),
    (CATALOG_PATH, catalog),
    (RUNBOOK_CARD_PATH, runbook_card_file),
    (RUNBOOK_MANIFEST_PATH, runbook_manifest_file),
]
for _path, _endpoint_function in ROUTES:
    router.add_api_route(_path, _endpoint_function, methods=["GET", "HEAD"])
    router.add_api_route(_path, _preflight, methods=["OPTIONS"])
