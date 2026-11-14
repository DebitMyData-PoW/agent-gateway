"""The MCP server as an OAuth protected resource (api/mcp_auth.py), against PostgreSQL: its
metadata, signing in with an access token from the authorization server, ``dmd_whoami``, and
every refusal. Without a token, everything works as before.
"""

from __future__ import annotations

import io
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from dmd_gateway.oauth import keys
from dmd_gateway.oauth.tokens import issue_token

from .conftest import TEST_DATABASE_URL
from .fakes import AGENT_KEY
from .test_identity_status import issue, run
from .test_oauth import AUTH, ISS, MCP, live, token_request
from .test_owner_key_tool import _tool
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import ISSUER, clean_chain, engine  # noqa: F401

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

HOST = {"Accept": "application/json, text/event-stream", "Host": "localhost:8000"}
METADATA = "/.well-known/oauth-protected-resource/agenticsuperhighway"
OLD_MCP = "http://localhost:8000/mcp"


@pytest.fixture
def key_path(tmp_path: Path) -> Path:
    path = tmp_path / "oauth.pem"
    keys.write_new_key(path)
    return path


@pytest.fixture
def gateway(ready_registry: Path, engine: Engine, key_path: Path) -> Iterator[TestClient]:  # noqa: F811
    overrides = {"auth_host": AUTH, "oauth_key_path": key_path}
    with _app_client(
        ready_registry, engine, issuer_key=ISSUER, settings_overrides=overrides
    ) as client:
        yield client


def rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any],
    token: str | None = None,
    *,
    path: str = "/agenticsuperhighway",
    authorization: str | None = None,
) -> Any:
    headers = dict(HOST)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if authorization is not None:
        headers["Authorization"] = authorization
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return client.post(path, json=body, headers=headers)


def whoami(client: TestClient, token: str | None = None, **kwargs: Any) -> Any:
    return rpc(client, "tools/call", {"name": "dmd_whoami", "arguments": {}}, token, **kwargs)


def sign_in(client: TestClient, did: str, **fields: str) -> str:
    response = token_request(client, live(did), **fields)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def refused(response: Any, words: str) -> None:
    assert response.status_code == 401, response.text
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith('Bearer error="invalid_token"')
    assert f'resource_metadata="http://localhost:8000{METADATA}"' in challenge or (
        'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert words in response.json()["error_description"]
    assert ISS not in challenge and MCP not in response.text  # never what it expects


def test_protected_resource_metadata(gateway: TestClient) -> None:
    response = gateway.get(METADATA)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.json() == {
        "resource": MCP,
        "authorization_servers": [ISS],
        "scopes_supported": ["did:read"],
        "bearer_methods_supported": ["header"],
        "resource_name": "DebitMyData Agentic Superhighway",
        "resource_documentation": "http://localhost:8000/llms.txt",
    }
    old = gateway.get("/.well-known/oauth-protected-resource/mcp").json()
    assert old["resource"] == OLD_MCP
    assert gateway.get("/.well-known/oauth-protected-resource").status_code == 404
    # The authorization server's host shows only the authorization server.
    assert gateway.get(METADATA, headers={"host": AUTH}).status_code == 404


def test_signed_in_agent_is_who_its_token_says(gateway: TestClient) -> None:
    issued = issue(gateway)
    did = issued["did"]
    token = sign_in(gateway, did)
    response = whoami(gateway, token)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    answer = result["structuredContent"]
    assert answer["signed_in_as"] == did
    assert answer["scope"] == "did:read"
    assert answer["identity"]["did"] == did
    assert answer["identity"]["status"] == "active"
    assert answer["identity"]["identity_id"] == issued["identity_id"]
    assert "entitlements" in answer
    assert datetime.fromisoformat(answer["token_expires_at"]) > datetime.now(UTC)

    # A token for /mcp, the first address, works at both addresses (the same server).
    old = sign_in(gateway, did, resource=OLD_MCP)
    for path in ("/mcp", "/agenticsuperhighway"):
        response = whoami(gateway, old, path=path)
        assert response.json()["result"]["structuredContent"]["signed_in_as"] == did


def test_without_a_token_everything_works_as_before(gateway: TestClient) -> None:
    listed = rpc(gateway, "tools/list", {})
    assert listed.status_code == 200, listed.text
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"dmd_whoami", "dmd_discover", "dmd_create_did_payment_quote"} <= names
    discover = rpc(gateway, "tools/call", {"name": "dmd_discover", "arguments": {}})
    assert not discover.json()["result"].get("isError")

    result = whoami(gateway).json()["result"]
    assert result["isError"] is True
    error = result["content"][0]["text"]
    assert '"code": "sign_in_required"' in error
    assert ISS in error and f"http://localhost:8000{METADATA}" in error


def test_invalid_tokens_are_refused(gateway: TestClient, key_path: Path, engine: Engine) -> None:  # noqa: F811
    did = issue(gateway)["did"]
    key = keys.load_private_key(key_path)
    now = datetime.now(UTC)

    def token(**changes: Any) -> str:
        claims = {
            "issuer": ISS,
            "did": did,
            "audience": MCP,
            "scopes": ("did:read",),
            "ttl_seconds": 600,
            "now": now,
            **changes,
        }
        signing_key = claims.pop("key", key)
        return issue_token(signing_key, **claims).access_token

    refused(whoami(gateway, "not-a-token"), "not valid for this server")
    refused(whoami(gateway, token(now=now - timedelta(hours=1))), "Token has expired")
    refused(whoami(gateway, token(audience="https://elsewhere.example/mcp")), "not valid")
    refused(whoami(gateway, token(issuer="https://auth.elsewhere.example")), "not valid")
    other = generate_private_key(public_exponent=65537, key_size=2048)
    refused(whoami(gateway, token(key=other)), "not valid")
    refused(whoami(gateway, token(did="did:web:unknown.example:agents:x")), "not valid")
    refused(whoami(gateway, authorization="Basic dXNlcjpwYXNz"), "Authorization: Bearer")
    refused(whoami(gateway, authorization="Bearer "), "Authorization: Bearer")
    # The token itself must not be the client's assertion either.
    refused(whoami(gateway, live(did)), "not valid")
    # The old address points to its own metadata.
    refused(whoami(gateway, "not-a-token", path="/mcp"), "not valid")

    # A valid token stops working as soon as its DID-LLM is suspended.
    good = sign_in(gateway, did)
    assert whoami(gateway, good).status_code == 200
    code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code == 0
    refused(whoami(gateway, good), "This DID-LLM is suspended")


def test_discovery_files_offer_oauth_once_sign_in_is_open(gateway: TestClient) -> None:
    for path in ("/.well-known/mcp", "/.well-known/mcp/server-card.json"):
        document = gateway.get(path).json()
        assert document["endpoints"][0]["authentication"] == ["oauth2.1"], path
    card = gateway.get("/.well-known/mcp/server-card.json").json()
    assert "dmd_whoami" in {tool["name"] for tool in card["tools"]}
    llms = gateway.get("/llms.txt").text
    assert "## Signing in with your DID-LLM (OAuth, optional)" in llms
    assert f"`aud` `{ISS}/token`" in llms and f"`resource={MCP}`" in llms
    assert f"http://localhost:8000{METADATA}" in llms


def test_tokens_are_ignored_while_sign_in_is_closed(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    with _app_client(ready_registry, engine, issuer_key=ISSUER) as closed:
        assert closed.get(METADATA).status_code == 404
        listed = rpc(closed, "tools/list", {}, "anything")
        assert listed.status_code == 200, listed.text
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert "dmd_whoami" not in names and "dmd_discover" in names
        manifest = closed.get("/.well-known/mcp").json()
        assert manifest["endpoints"][0]["authentication"] == []
        assert "Signing in" not in closed.get("/llms.txt").text


# --------------------------------------------------------------------------- the owner-key tool


class _Answer(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _through(gateway: TestClient) -> Callable[..., _Answer]:
    """urllib's urlopen, answered by the gateway: the tool's own requests, unchanged."""

    def urlopen(request: urllib.request.Request, timeout: float | None = None) -> _Answer:
        parts = urlsplit(request.full_url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {**dict(request.header_items()), "host": parts.netloc}
        response = gateway.request(
            request.get_method(), path, content=request.data, headers=headers
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status_code,
                response.reason_phrase,
                response.headers,  # type: ignore[arg-type]
                io.BytesIO(response.content),
            )
        return _Answer(response.status_code, response.content)

    return urlopen


def test_the_owner_key_tool_signs_in_end_to_end(
    gateway: TestClient,
    engine: Engine,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool's whole sign-in against the real gateway: the DID document, the authorization
    server's metadata, the signed request, the token, and the MCP server's dmd_whoami."""
    did = issue(gateway)["did"]
    key_file = tmp_path / "agent.key"
    key_file.write_bytes(
        AGENT_KEY.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_file, 0o600)
    tool = _tool()
    tool.API, tool.AUTH = "http://localhost:8000", ISS
    monkeypatch.setattr(urllib.request, "urlopen", _through(gateway))

    tool.whoami(str(key_file), did)
    out = capsys.readouterr().out
    assert f"The Agentic Superhighway says: signed in as {did}" in out
    assert "status: active" in out and "scope: did:read" in out

    tool.token(str(key_file), did)
    assert "valid 10 minutes" in capsys.readouterr().out

    code, _ = run(engine, "did", "suspend", did, "--reason", "Test", "--by", "T")
    assert code == 0
    with pytest.raises(SystemExit, match="suspended"):
        tool.whoami(str(key_file), did)
