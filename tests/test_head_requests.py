"""HEAD answered as GET without the body (api/head_requests.py): uptime services, link checkers
and `curl -I` ask with HEAD. The same status and headers as GET, no body; an address without
GET still refuses HEAD; an address that answers HEAD itself keeps its answer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

PAGES = ["/healthz", "/readyz", "/manage", "/get-did", "/llms.txt", "/.well-known/security.txt"]
SAME = ("content-type", "content-length", "content-security-policy", "cache-control")


@pytest.mark.parametrize("path", PAGES)
def test_head_is_get_without_the_body(client: TestClient, path: str) -> None:
    got = client.get(path)
    head = client.head(path)
    assert head.status_code == got.status_code  # /readyz: 503 here (no database), both ways
    assert head.content == b""
    for name in SAME:
        assert head.headers.get(name) == got.headers.get(name), name
    assert head.headers["x-content-type-options"] == "nosniff"


def test_an_address_without_get_still_refuses_head(client: TestClient) -> None:
    head = client.head("/agenticsuperhighway/a2a")  # A2A: POST only
    assert head.status_code == 405
    assert head.headers["allow"] == "POST"
    assert client.get("/agenticsuperhighway/a2a").status_code == 405


def test_an_address_that_answers_head_itself_keeps_its_answer(client: TestClient) -> None:
    head = client.head("/.well-known/agent-card.json")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["etag"] == client.get("/.well-known/agent-card.json").headers["etag"]
