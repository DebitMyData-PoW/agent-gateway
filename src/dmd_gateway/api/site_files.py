"""What crawlers, AI assistants and agents look for at a site's root, at both of the gateway's
names (the API's and the app's, hosts.py):

    GET /robots.txt               everyone welcome: search engines, AI search and assistants,
                                  agents; the Content Signals (contentsignals.org) say what the
                                  content may be used for; and where the sitemap is
    GET /sitemap.xml              the pages at this name
    GET /.well-known/api-catalog  RFC 9727: the APIs here, as a linkset (RFC 9264): the REST API
                                  (its OpenAPI description, its docs, llms.txt, its status) and
                                  the MCP server (its Server Card)

The staging server is a test server: its robots.txt asks crawlers not to list it, and it has
no sitemap. Agents and the API are not affected (robots.txt is for crawlers).

The pages for people send a ``Link`` header (RFC 8288) to the same places: ``link_header``.
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from dmd_gateway.api import brand
from dmd_gateway.api.get_did_page import APP_ROOT, mcp_url, page_url
from dmd_gateway.api.hosts import is_app
from dmd_gateway.api.server_card import card_url
from dmd_gateway.settings import Settings

router = APIRouter(include_in_schema=False)

ROBOTS_PATH = "/robots.txt"
SITEMAP_PATH = "/sitemap.xml"
API_CATALOG_PATH = "/.well-known/api-catalog"
API_CATALOG_TYPE = 'application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"'
OPENAPI_PATH = "/openapi.json"
STATUS_PATH = "/healthz"
# Search engines, AI search and AI assistants may use the content, and AI models may learn from
# it: being known to agents and the models behind them is what the DID-LLM is for.
CONTENT_SIGNALS = "search=yes, ai-input=yes, ai-train=yes"
CACHE = "public, max-age=3600"


def _base(settings: Settings) -> str:
    return settings.public_base_url.rstrip("/")


def _here(request: Request) -> str:
    """This name's own address: the app's on the app's host, the API's elsewhere."""
    settings: Settings = request.app.state.settings
    if is_app(request):
        return f"https://{settings.app_host}"
    return _base(settings)


def crawlable(settings: Settings) -> bool:
    return not settings.staging_test_tools


def pages(request: Request) -> list[str]:
    """The pages at this name, as people and crawlers reach them (nothing that redirects)."""
    settings: Settings = request.app.state.settings
    policies = [page.path for page in request.app.state.policy_pages.values()]
    people = [APP_ROOT, brand.DIRECTORY_PATH, brand.POLICIES_PATH, *policies]
    developers = [brand.ADVANCED_PATH, brand.MCP_PATH, brand.LLMS_PATH]
    if not settings.app_host:  # one name for everything
        return [brand.GET_DID_PATH, *people[1:], *developers]
    if is_app(request):
        return people
    return developers  # the pages for people lead to the app's name from here


def link_header(settings: Settings) -> str:
    """For a page's ``Link`` header (RFC 8288): where an agent finds the APIs and the text."""
    base = _base(settings)
    return ", ".join(
        (
            f'<{base}{API_CATALOG_PATH}>; rel="api-catalog"',
            f'<{base}{OPENAPI_PATH}>; rel="service-desc"; type="application/openapi+json"',
            f'<{base}{brand.ADVANCED_PATH}>; rel="service-doc"; type="text/html"',
            f'<{base}{brand.LLMS_PATH}>; rel="describedby"; type="text/markdown"',
            f'<{page_url(settings)}>; rel="alternate"; type="text/markdown"',
        )
    )


def api_catalog(settings: Settings) -> dict[str, Any]:
    base = _base(settings)
    return {
        "linkset": [
            {
                "anchor": f"{base}/v1/",
                "service-desc": [
                    {"href": f"{base}{OPENAPI_PATH}", "type": "application/openapi+json"}
                ],
                "service-doc": [
                    {"href": f"{base}{brand.ADVANCED_PATH}", "type": "text/html"},
                    {"href": f"{base}{brand.LLMS_PATH}", "type": "text/markdown"},
                ],
                "status": [{"href": f"{base}{STATUS_PATH}", "type": "application/json"}],
            },
            {
                "anchor": mcp_url(settings),
                "service-desc": [
                    {"href": card_url(settings), "type": "application/mcp-server-card+json"}
                ],
                "service-doc": [{"href": f"{base}{brand.LLMS_PATH}", "type": "text/markdown"}],
            },
        ]
    }


@router.get(ROBOTS_PATH)
def robots(request: Request) -> PlainTextResponse:
    settings: Settings = request.app.state.settings
    if not crawlable(settings):
        text = (
            "# DebitMyData staging: a test server, with test networks and test DID-LLMs only.\n"
            f"# The real service is {brand.PRODUCTION_SITE}\n"
            "User-agent: *\nDisallow: /\n"
        )
    else:
        text = (
            "# DebitMyData: search engines, AI assistants and agents are welcome.\n"
            f"# Agents: {_base(settings)}{brand.LLMS_PATH} says how to get a DID-LLM.\n"
            f"User-agent: *\nContent-Signal: {CONTENT_SIGNALS}\nAllow: /\n\n"
            f"Sitemap: {_here(request)}{SITEMAP_PATH}\n"
        )
    return PlainTextResponse(text, headers={"Cache-Control": CACHE})


@router.get(SITEMAP_PATH)
def sitemap(request: Request) -> Response:
    if not crawlable(request.app.state.settings):
        return Response(status_code=404)
    here = _here(request)
    urls = "".join(f"<url><loc>{escape(here + path)}</loc></url>" for path in pages(request))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n'
    )
    return Response(xml, media_type="application/xml", headers={"Cache-Control": CACHE})


def _api_catalog(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    link = f'<{_base(settings)}{API_CATALOG_PATH}>; rel="api-catalog"'
    headers = {"Cache-Control": CACHE, "Link": link, "Access-Control-Allow-Origin": "*"}
    if request.method == "HEAD":
        return Response(media_type=API_CATALOG_TYPE, headers=headers)
    return JSONResponse(api_catalog(settings), media_type=API_CATALOG_TYPE, headers=headers)


router.add_api_route(API_CATALOG_PATH, _api_catalog, methods=["GET", "HEAD"])
