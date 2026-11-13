"""The Agentic Superhighway's Server Card, the AI Catalog that points to it, the discovery
runbook's two files and server.json (its listing in the official MCP Registry): MCP clients
and crawlers find the server by themselves, and the listing says the same as the card."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient
from mcp_types.version import LATEST_PROTOCOL_VERSION
from sqlalchemy import create_engine

from dmd_gateway import __version__
from dmd_gateway.api.server_card import (
    DESCRIPTION,
    TEST_DESCRIPTION,
    TITLE,
    registry_entry,
    server_card,
    server_name,
)
from dmd_gateway.main import create_app
from dmd_gateway.settings import Settings
from tests.conftest import REPO_ROOT, UNREACHABLE_DB

CARD = "/agenticsuperhighway/server-card"
CARD_PATHS = [CARD, "/mcp/server-card"]
CATALOG = "/.well-known/ai-catalog.json"
RUNBOOK_CARD = "/.well-known/mcp/server-card.json"
RUNBOOK_MANIFEST = "/.well-known/mcp"
MCP = "http://localhost:8000/agenticsuperhighway"
NAME = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")  # the card's and the Registry's rule


def production(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "environment": "production",
            "public_base_url": "https://api.debitmydata.io",
            "app_host": "id.debitmydata.io",
        }
    )


@pytest.mark.parametrize("path", CARD_PATHS)
def test_the_card_says_what_the_server_is_and_how_to_connect(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/mcp-server-card+json"
    card = response.json()
    assert card["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/v1/")
    assert card["name"] == "io.debitmydata/agenticsuperhighway-development"
    assert card["version"] == __version__
    assert card["description"] == TEST_DESCRIPTION
    assert card["repository"]["url"] == "https://github.com/DebitMyData-PoW/agent-gateway"
    (remote,) = card["remotes"]
    assert remote["type"] == "streamable-http"
    assert remote["url"] == "http://localhost:8000/agenticsuperhighway"
    assert {"2025-06-18", "2025-11-25"} <= set(remote["supportedProtocolVersions"])
    # No tools: an MCP client asks the server for them (tools/list).
    assert not {"tools", "resources", "prompts", "capabilities"} & card.keys()


def test_the_card_is_public_metadata_for_every_origin(client: TestClient) -> None:
    headers = client.get(CARD).headers
    assert headers["access-control-allow-origin"] == "*"
    assert headers["access-control-expose-headers"] == "ETag"
    assert headers["cache-control"] == "public, max-age=3600"
    assert headers["x-content-type-options"] == "nosniff"
    preflight = client.options(CARD)
    assert preflight.status_code == 204
    assert "If-None-Match" in preflight.headers["access-control-allow-headers"]


def test_a_client_that_has_the_card_learns_it_did_not_change(client: TestClient) -> None:
    etag = client.get(CARD).headers["etag"]
    assert client.get(CARD, headers={"If-None-Match": etag}).status_code == 304
    assert client.get(CARD, headers={"If-None-Match": f"W/{etag}"}).status_code == 304
    assert client.get(CARD, headers={"If-None-Match": '"other"'}).status_code == 200


def test_the_ai_catalog_points_to_the_card(client: TestClient) -> None:
    response = client.get(CATALOG)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/ai-catalog+json"
    assert response.headers["access-control-allow-origin"] == "*"
    catalog = response.json()
    assert catalog["specVersion"] == "1.0"
    (entry,) = catalog["entries"]
    assert entry == {
        "identifier": "urn:air:debitmydata.io:mcp:agenticsuperhighway-development",
        "type": "application/mcp-server-card+json",
        "url": f"http://localhost:8000{CARD}",
    }
    assert client.options(CATALOG).status_code == 204


def test_the_runbooks_day_1_checks_pass(client: TestClient) -> None:
    """The runbook's Monday gate: the card's mcp_version, the manifest's first endpoint, and
    application/json (asked with HEAD, as `curl -sI` does)."""
    card = client.get(RUNBOOK_CARD)
    assert card.status_code == 200
    assert card.json()["mcp_version"] == LATEST_PROTOCOL_VERSION
    manifest = client.get(RUNBOOK_MANIFEST)
    assert manifest.status_code == 200
    assert manifest.json()["endpoints"][0]["url"] == MCP
    for path in (RUNBOOK_CARD, RUNBOOK_MANIFEST):
        head = client.head(path)
        assert head.status_code == 200, path
        assert head.headers["content-type"] == "application/json", path
        assert head.content == b""
        headers = client.get(path).headers
        assert headers["cache-control"] == "public, max-age=300"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["access-control-allow-origin"] == "*"
        assert client.options(path).status_code == 204


def test_the_runbooks_card_lists_the_tools_the_server_lists(client: TestClient) -> None:
    card = client.get(RUNBOOK_CARD).json()
    assert card["server_info"]["version"] == __version__
    assert card["server_card"] == f"http://localhost:8000{CARD}"  # the standard card
    (endpoint,) = card["endpoints"]
    assert endpoint["url"] == MCP and endpoint["transport"] == "streamable-http"
    assert endpoint["authentication"] == []  # agents need no account
    live = asyncio.run(client.app.state.mcp.list_tools())
    assert [tool["name"] for tool in card["tools"]] == [tool.name for tool in live]
    assert "dmd_create_did_payment_quote" in {tool["name"] for tool in card["tools"]}
    assert all(tool["description"] for tool in card["tools"])


def test_the_mcp_server_itself_is_unchanged(client: TestClient) -> None:
    # The card's address is beside the MCP server's, not in its place.
    response = client.get(
        "/agenticsuperhighway",
        headers={"Host": "localhost:8000", "Accept": "application/json"},
    )
    assert response.status_code == 406  # the MCP server's own answer, as before


def test_llms_txt_gives_the_card(client: TestClient) -> None:
    assert f"http://localhost:8000{CARD}" in client.get("/llms.txt").text


def test_the_app_address_serves_them_too(settings: Settings) -> None:
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    app = create_app(
        settings.model_copy(update={"app_host": "id.debitmydata.io"}), environ={}, engine=engine
    )
    with TestClient(app, follow_redirects=False) as client:
        for path in (CARD, CATALOG, RUNBOOK_CARD, RUNBOOK_MANIFEST):
            assert client.get(path, headers={"Host": "id.debitmydata.io"}).status_code == 200


def test_production_has_the_real_name_and_test_servers_say_they_are_tests(
    settings: Settings,
) -> None:
    real = server_card(production(settings))
    assert real["name"] == server_name(production(settings)) == "io.debitmydata/agenticsuperhighway"
    assert real["title"] == TITLE and real["description"] == DESCRIPTION
    assert real["remotes"][0]["url"] == "https://api.debitmydata.io/agenticsuperhighway"
    assert real["websiteUrl"] == "https://id.debitmydata.io/"
    assert all(icon["src"].startswith("https://") for icon in real["icons"])
    staging = settings.model_copy(update={"environment": "staging"})
    assert server_name(staging) == "io.debitmydata/agenticsuperhighway-staging"
    assert server_card(staging)["title"] == f"{TITLE} (staging)"
    for card in (real, server_card(staging)):
        assert NAME.match(card["name"]) and 3 <= len(card["name"]) <= 200
        assert 1 <= len(card["description"]) <= 100
        assert 1 <= len(card["title"]) <= 100


def test_server_json_lists_the_production_server_as_its_card_describes_it(
    settings: Settings,
) -> None:
    """server.json (repository root) is what `mcp-publisher publish` sends to the MCP Registry.
    After a change here, write it again with production settings (docs/production.md,
    section 13)."""
    listed = json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))
    assert listed == registry_entry(production(settings))
    card = server_card(production(settings))
    for field in ("name", "title", "description", "version", "websiteUrl", "repository", "icons"):
        assert listed[field] == card[field], field
    assert listed["remotes"][0]["url"] == card["remotes"][0]["url"]
