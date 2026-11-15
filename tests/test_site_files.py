"""What crawlers, AI assistants and agents read about the site (api/site_files.py and the Get a
DID-LLM page's head): robots.txt with its Content Signals, sitemap.xml, the RFC 9727 API
catalog, the page's ``Link`` header, its schema.org structured data (the DID-LLM as a Product
with its Offer, DebitMyData, the site), its Open Graph tags and its Markdown version."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dmd_gateway.api import get_did_page, site_files
from dmd_gateway.api.get_did_page import head_tags, wants_markdown
from dmd_gateway.settings import Settings

from .test_get_did_page import APP_HOST, app_client  # noqa: F401
from .test_staging_tools import staging_client  # noqa: F401

API = {"host": "localhost:8000"}  # the API's name, next to the app's (id.debitmydata.io)
BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def structured(page: str) -> list[dict[str, Any]]:
    found = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    assert found, "no structured data"
    return json.loads(found.group(1))


def by_type(data: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    (item,) = [item for item in data if item["@type"] == kind]
    return item


# --------------------------------------------------------------------------- robots and sitemap


def test_robots_welcomes_crawlers_and_agents_with_content_signals(app_client: TestClient) -> None:  # noqa: F811
    for headers, here in (({}, f"https://{APP_HOST}"), (API, "http://localhost:8000")):
        response = app_client.get("/robots.txt", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        lines = response.text.splitlines()
        assert "User-agent: *" in lines and "Allow: /" in lines
        assert "Disallow: /" not in lines
        assert "Content-Signal: search=yes, ai-input=yes, ai-train=yes" in lines
        # Each name gives its own sitemap.
        assert f"Sitemap: {here}/sitemap.xml" in lines
        assert "http://localhost:8000/llms.txt" in response.text


def test_the_sitemap_lists_the_pages_at_each_name_none_that_moves(app_client: TestClient) -> None:  # noqa: F811
    for headers, here, expected in (
        ({}, f"https://{APP_HOST}", ["/", "/directory", "/policies", "/policies/refunds"]),
        (API, "http://localhost:8000", ["/docs/advanced", "/agenticsuperhighway", "/llms.txt"]),
    ):
        response = app_client.get("/sitemap.xml", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/xml")
        urls = re.findall(r"<loc>(.*?)</loc>", response.text)
        paths = [url.removeprefix(here) for url in urls]
        assert all(url.startswith(here) for url in urls)
        assert set(expected) <= set(paths)
        # Each one is at this name: none sends people elsewhere (the directory needs the
        # database, which these tests do not have).
        for path in paths:
            response = app_client.get(path, headers=headers | BROWSER, follow_redirects=False)
            assert response.status_code in (200, 503), path


def test_a_single_name_lists_everything(client: TestClient) -> None:
    paths = re.findall(r"<loc>http://localhost:8000(.*?)</loc>", client.get("/sitemap.xml").text)
    assert paths[0] == "/get-did" and {"/directory", "/docs/advanced", "/llms.txt"} <= set(paths)


def test_staging_asks_crawlers_to_stay_away(staging_client: TestClient) -> None:  # noqa: F811
    robots = staging_client.get("/robots.txt").text
    assert "User-agent: *\nDisallow: /\n" in robots and "test server" in robots
    assert "Sitemap" not in robots
    assert staging_client.get("/sitemap.xml").status_code == 404
    # Agents and the API are not affected.
    assert staging_client.get("/llms.txt").status_code == 200


# --------------------------------------------------------------------------- the API catalog


def test_the_api_catalog_lists_the_rest_api_and_the_mcp_server(app_client: TestClient) -> None:  # noqa: F811
    for headers in ({}, API):
        response = app_client.get("/.well-known/api-catalog", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"] == site_files.API_CATALOG_TYPE
        assert response.headers["access-control-allow-origin"] == "*"
        assert 'rel="api-catalog"' in response.headers["link"]
        rest, mcp = response.json()["linkset"]
        assert rest["anchor"] == "http://localhost:8000/v1/"
        assert rest["service-desc"] == [
            {"href": "http://localhost:8000/openapi.json", "type": "application/openapi+json"}
        ]
        assert {link["href"] for link in rest["service-doc"]} == {
            "http://localhost:8000/docs/advanced",
            "http://localhost:8000/llms.txt",
        }
        assert rest["status"][0]["href"] == "http://localhost:8000/healthz"
        assert mcp["anchor"] == "http://localhost:8000/agenticsuperhighway"
        assert mcp["service-desc"][0]["type"] == "application/mcp-server-card+json"
        # Every address in it answers.
        for item in (rest, mcp):
            for relation in ("service-desc", "service-doc", "status"):
                for link in item.get(relation, []):
                    path = link["href"].removeprefix("http://localhost:8000")
                    assert app_client.get(path, headers=API).status_code == 200, path
    head = app_client.head("/.well-known/api-catalog")
    assert head.status_code == 200 and not head.content and "api-catalog" in head.headers["link"]


# --------------------------------------------------------------------------- the page


def test_the_page_describes_the_did_llm_for_agents_and_search_engines(
    app_client: TestClient,  # noqa: F811
) -> None:
    response = app_client.get("/", headers={"accept": "text/html,*/*;q=0.8"})
    page = response.text
    data = structured(page)
    assert all(item["@context"] == "https://schema.org" for item in data)
    product = by_type(data, "Product")
    assert product["name"] == "DebitMyData DID-LLM"
    assert product["description"].startswith("A verifiable identity, a W3C did:web DID")
    assert product["image"] == f"https://{APP_HOST}/app/icon-512.png?v=3"
    assert product["url"] == f"https://{APP_HOST}/"
    offer = product["offers"]
    assert (offer["@type"], offer["price"], offer["priceCurrency"]) == ("Offer", "25.00", "USD")
    assert offer["availability"] == "https://schema.org/InStock"
    assert offer["url"] == f"https://{APP_HOST}/#start"  # the page's own start section
    assert "id=start" in page or 'id="start"' in page
    # The same price as the page shows people.
    assert "id=price>$25.00 USD, paid once<" in page
    organization = by_type(data, "Organization")
    assert organization["name"] == "DebitMyData"
    assert organization["url"] == "https://debitmydata.com/"
    assert offer["seller"] == {"@id": organization["@id"]}
    website = by_type(data, "WebSite")
    assert website["url"] == f"https://{APP_HOST}/"
    assert website["publisher"] == {"@id": organization["@id"]}
    # The images exist.
    assert app_client.get(product["image"].removeprefix(f"https://{APP_HOST}")).status_code == 200
    assert app_client.get("/logo.png", headers=API).status_code == 200

    for tag in (
        f'<link rel=canonical href="https://{APP_HOST}/">',
        '<meta property="og:type" content="product">',
        '<meta property="og:title" content="Get your DebitMyData DID-LLM">',
        '<meta property="product:price:amount" content="25.00">',
        '<meta property="product:price:currency" content="USD">',
    ):
        assert tag in page, tag
    links = response.headers["link"]
    for relation in ("api-catalog", "service-desc", "service-doc", "describedby", "alternate"):
        assert f'rel="{relation}"' in links, relation
    assert response.headers["vary"] == "Accept"


def test_the_structured_data_cannot_end_its_script_early(settings: Settings) -> None:
    tags = head_tags(settings, ("25.00", "</script><script>alert(1)"))
    script = tags[tags.index('<script type="application/ld+json">') :]
    assert script.count("</script>") == 1 and "<script>alert" not in script


def test_agents_that_ask_for_markdown_get_it(app_client: TestClient) -> None:  # noqa: F811
    for accept in ("text/markdown", "text/markdown, text/html;q=0.9", "text/plain, text/markdown"):
        response = app_client.get("/", headers={"accept": accept})
        assert response.status_code == 200, accept
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"
        assert response.text.startswith("# DebitMyData DID-LLM")
        assert "It costs $25.00 USD, paid once." in response.text
        assert response.headers["vary"] == "Accept" and "api-catalog" in response.headers["link"]
    for accept in (BROWSER["accept"], "*/*", "", "text/html, text/markdown", "text/markdown;q=0"):
        response = app_client.get("/", headers={"accept": accept})
        assert response.headers["content-type"].startswith("text/html"), accept


@pytest.mark.parametrize(
    ("accept", "markdown"),
    [
        ("text/markdown", True),
        ("text/markdown;q=0.8, text/html;q=0.5", True),
        ("text/html;q=0.5, text/markdown;q=0.5", False),
        ("text/markdown;q=0.5, text/html;q=0.5", True),
        ("text/html", False),
        ("text/markdown;q=nonsense", False),
        ("*/*", False),
        ("", False),
    ],
)
def test_wants_markdown(accept: str, markdown: bool) -> None:
    assert wants_markdown(accept) is markdown


def test_the_page_script_shows_the_same_price_text() -> None:
    """The page's script writes the price again from the API: with two decimals, as the
    server wrote it (an agent compares the price it got with the page's, word for word)."""
    script = (Path(get_did_page.__file__).parent.parent / "static" / "get-did.js").read_text()
    assert "minimumFractionDigits: 2, maximumFractionDigits: 2" in script
    assert '`${currency === "USD" ? "$" : ""}${amount} ${currency}, paid once`' in script


@pytest.mark.parametrize(
    ("price", "text"),
    [
        (("25.00", "USD"), "$25.00 USD, paid once"),
        (("30.50", "EUR"), "30.50 EUR, paid once"),
    ],
)
def test_price_text(price: tuple[str, str], text: str) -> None:
    assert get_did_page.price_text(price) == text
