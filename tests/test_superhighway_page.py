"""The page a web browser gets at the MCP server's address; MCP clients still get the server."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from dmd_gateway.api.superhighway_page import page_html, wants_page
from dmd_gateway.settings import Settings

BROWSER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
HOST = {"Host": "localhost:8000"}


def scope(method: str, accept: str | None) -> dict[str, object]:
    headers = [(b"accept", accept.encode())] if accept is not None else []
    return {"type": "http", "method": method, "headers": headers}


@pytest.mark.parametrize(
    ("method", "accept", "page"),
    [
        ("GET", BROWSER, True),  # Chrome, Firefox, Safari
        ("HEAD", "text/html", True),
        ("GET", "text/event-stream", False),  # an MCP client opening its message stream
        ("GET", "text/html, text/event-stream", False),
        ("GET", "application/json", False),
        ("GET", "*/*", False),  # curl and most HTTP libraries
        ("GET", None, False),
        ("POST", BROWSER, False),  # every MCP message is a POST
    ],
)
def test_only_a_browser_opening_the_address_gets_the_page(
    method: str, accept: str | None, page: bool
) -> None:
    assert wants_page(scope(method, accept)) is page


@pytest.mark.parametrize("path", ["/agenticsuperhighway", "/mcp"])
def test_a_browser_gets_a_page_saying_what_the_address_is(client: TestClient, path: str) -> None:
    response = client.get(path, headers={**HOST, "Accept": BROWSER})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<h1>Agentic Superhighway</h1>" in body
    assert "<code>http://localhost:8000/agenticsuperhighway</code>" in body  # the address to use
    assert 'href="/llms.txt"' in body and 'href="/v1/discovery"' in body
    assert 'href="/get-did"' in body
    # The same address answers MCP clients: a cache must never hand them this page.
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["vary"] == "Accept"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_mcp_clients_still_get_the_mcp_server(client: TestClient) -> None:
    # A GET that asks for neither HTML nor the event stream: the MCP server's own answer.
    response = client.get("/agenticsuperhighway", headers={**HOST, "Accept": "application/json"})
    assert response.status_code == 406
    assert "Agentic Superhighway" not in response.text


def test_the_test_server_note_shows_away_from_production_only(settings: Settings) -> None:
    assert "<strong>Test server.</strong>" in page_html(settings)
    production = settings.model_copy(update={"environment": "production"})
    assert "<strong>Test server.</strong>" not in page_html(production)


def test_the_page_says_what_is_sold_here_with_the_price_word_for_word(client: TestClient) -> None:
    """Agent checkers that shop through the MCP server look for the price on the page at its
    address: the same text as on Get a DID-LLM, in the API and in the tools."""
    body = client.get("/agenticsuperhighway", headers={**HOST, "Accept": BROWSER}).text
    assert "<h2>Sold here</h2>" in body
    assert "<strong>DebitMyData DID-LLM</strong>: <span class=price>$25.00 USD, paid once" in body
    assert 'href="http://localhost:8000/get-did#start">Buy a DID-LLM</a>' in body
    assert "<code>debitmydata_open_checkout</code>" in body
    found = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
    product, organization = json.loads(found.group(1))
    assert (product["@type"], product["name"]) == ("Product", "DebitMyData DID-LLM")
    assert (product["offers"]["price"], product["offers"]["priceCurrency"]) == ("25.00", "USD")
    assert product["offers"]["url"] == "http://localhost:8000/get-did#start"
    assert organization["@type"] == "Organization"
