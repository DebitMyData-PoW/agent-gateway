"""Every answer carries HSTS (when the gateway's address is https) and nosniff, whatever made
it: the API, a page, A2A, the MCP server, an error (api/security_headers.py). An answer's own
value is kept, never doubled."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from dmd_gateway.api.security_headers import HSTS
from dmd_gateway.main import create_app
from dmd_gateway.settings import Settings

from .conftest import UNREACHABLE_DB

PATHS = (
    "/healthz",  # the API
    "/manage",  # a page, with its own nosniff
    "/.well-known/agent-card.json",  # A2A
    "/no-such-page",  # an error
)


@pytest.fixture
def https_client(settings: Settings) -> Iterator[TestClient]:
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    secure = settings.model_copy(update={"public_base_url": "https://api.example.test"})
    with TestClient(create_app(secure, environ={}, engine=engine)) as client:
        yield client


def test_every_answer_over_https_has_hsts_and_nosniff(https_client: TestClient) -> None:
    for path in PATHS:
        response = https_client.get(path)
        assert response.headers.get_list("strict-transport-security") == [HSTS], path
        assert response.headers.get_list("x-content-type-options") == ["nosniff"], path
    mcp = https_client.post(
        "/agenticsuperhighway",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert mcp.headers["strict-transport-security"] == HSTS
    assert HSTS == "max-age=31536000"  # a year, this name only: no includeSubDomains


def test_no_hsts_over_plain_http(client: TestClient) -> None:
    response = client.get("/healthz")
    assert "strict-transport-security" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
