"""The identity endpoint (``/agenticsuperhighway/identity``): three read-only tools, every one
for a signed-in DID-LLM, with tokens of its own (api/mcp_auth.py, mcp_server.py); ending an
app's sign-in ends its access tokens at once; Google's domain-verification file.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from dmd_gateway.oauth import keys
from dmd_gateway.oauth.tokens import verify_token

from .conftest import TEST_DATABASE_URL
from .test_identity_status import issue
from .test_mcp_auth import HOST, rpc, sign_in, whoami
from .test_oauth import ISS, MCP
from .test_oauth_browser import (  # noqa: F401
    APP,
    CALLBACK,
    at_auth,
    code_for,
    gateway,
    pkce,
    token,
)
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import ISSUER, clean_chain, engine  # noqa: F401

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

IDENTITY = f"{MCP}/identity"
IDENTITY_PATH = "/agenticsuperhighway/identity"
METADATA = "/.well-known/oauth-protected-resource/agenticsuperhighway/identity"
TOOLS = {"debitmydata_whoami", "debitmydata_get_profile", "debitmydata_verify_agent"}


def call(client: TestClient, name: str, token: str | None, **arguments: Any) -> Any:
    return rpc(
        client,
        "tools/call",
        {"name": name, "arguments": arguments},
        token,
        path=IDENTITY_PATH,
    )


def result(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()["result"]
    assert not body.get("isError"), body
    return body["structuredContent"]


def test_only_three_read_only_tools_and_its_own_metadata(gateway: TestClient) -> None:  # noqa: F811
    meta = gateway.get(METADATA, headers=HOST)
    assert meta.status_code == 200
    assert meta.json()["resource"] == IDENTITY
    assert meta.json()["resource_name"] == "DebitMyData DID-LLM identity"
    assert meta.json()["authorization_servers"] == [ISS]
    assert meta.json()["scopes_supported"] == ["did:read"]
    llms = gateway.get("/llms.txt").text
    assert f"resource={IDENTITY}" in llms and "ends its access tokens at once" in llms

    # Every request needs a token, from the first one: the 401 that starts the client's
    # sign-in (initialize, tools/list, any tool, even one that does not exist here).
    for method, params in (
        (
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        ),
        ("tools/list", {}),
        *(
            ("tools/call", {"name": name, "arguments": {}})
            for name in (*TOOLS, "debitmydata_register_did")
        ),
    ):
        refused = rpc(gateway, method, params, path=IDENTITY_PATH)
        assert refused.status_code == 401, (method, params, refused.text)
        challenge = refused.headers["www-authenticate"]
        assert challenge == (
            f'Bearer resource_metadata="http://localhost:8000{METADATA}", scope="did:read"'
        )
    assert gateway.get(IDENTITY_PATH, headers=HOST).status_code == 401
    assert gateway.options(IDENTITY_PATH, headers=HOST).status_code != 401  # CORS preflight

    did = issue(gateway)["did"]
    identity = sign_in(gateway, did, resource=IDENTITY)
    listed = rpc(gateway, "tools/list", {}, identity, path=IDENTITY_PATH)
    assert listed.status_code == 200, listed.text
    tools = listed.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == TOOLS
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["_meta"]["securitySchemes"] == [{"type": "oauth2", "scopes": ["did:read"]}]
    verify = next(tool for tool in tools if tool["name"] == "debitmydata_verify_agent")
    assert set(verify["inputSchema"]["properties"]) == {"did"}  # nothing to sign here


def test_tokens_are_for_one_endpoint_or_the_other(gateway: TestClient) -> None:  # noqa: F811
    issued = issue(gateway)
    did = issued["did"]
    superhighway = sign_in(gateway, did)  # no resource: the Agentic Superhighway
    identity = sign_in(gateway, did, resource=IDENTITY)

    refused = call(gateway, "debitmydata_whoami", superhighway)
    assert refused.status_code == 401
    assert "not valid for this server" in refused.json()["error_description"]
    assert whoami(gateway, identity).status_code == 401
    assert whoami(gateway, superhighway).status_code == 200

    me = result(call(gateway, "debitmydata_whoami", identity))
    assert me["signed_in_as"] == did and me["scope"] == "did:read"

    profile = result(call(gateway, "debitmydata_get_profile", identity))
    assert profile["did"] == did and profile["identity_id"] == issued["identity_id"]
    assert profile["status"] == "active" and profile["owner_changed_at"] is None
    public = (
        "ed25519:" + issued["credential"]["credentialSubject"]["controller"]["publicKeyMultibase"]
    )
    assert profile["owners"] == [{"type": "key", "value": public}]
    assert [row["entitlement"] for row in profile["entitlements"]] == ["dmd_did_holder"]
    assert profile["directory_listing"] is None
    assert profile["credentials"] == [issued["credential_url"]]

    checked = result(call(gateway, "debitmydata_verify_agent", identity, did=did))
    assert checked["verified"] is True and checked["credential"]["signature_valid"] is True
    assert "challenge" not in checked
    unknown = result(
        call(gateway, "debitmydata_verify_agent", identity, did=did.rsplit(":", 1)[0] + ":01NOPE")
    )
    assert unknown["verified"] is False and unknown["found"] is False

    # Nothing else exists here, even signed in.
    other = call(gateway, "debitmydata_register_did", identity, registration_intent_id="x")
    assert other.status_code == 200
    assert other.json()["result"]["isError"] is True
    assert "Unknown tool" in other.json()["result"]["content"][0]["text"]


def test_ending_an_apps_sign_in_ends_its_access_tokens_at_once(gateway: TestClient) -> None:  # noqa: F811
    did = issue(gateway)["did"]
    public = keys.load_private_key(gateway.key_path).public_key()  # type: ignore[attr-defined]

    def signed_in(**changes: Any) -> dict[str, Any]:
        verifier, challenge_value = pkce()
        code = code_for(gateway, did, challenge_value, **changes)
        fields = {"resource": changes["resource"]} if "resource" in changes else {}
        exchanged = token(
            gateway,
            grant_type="authorization_code",
            code=code,
            redirect_uri=CALLBACK,
            code_verifier=verifier,
            **fields,
        )
        assert exchanged.status_code == 200, exchanged.text
        return exchanged.json()

    # Revoking the refresh token: the access token stops on its next request.
    tokens = signed_in()
    claims = verify_token(
        tokens["access_token"], public, issuer=ISS, audiences=[MCP], now=datetime.now(UTC)
    )
    assert claims["sid"].startswith("dmd_oauthfam_")
    assert whoami(gateway, tokens["access_token"]).status_code == 200
    # Another app cannot end it.
    other = {"token": tokens["refresh_token"], "client_id": "https://other.example/c.json"}
    assert at_auth(gateway, "POST", "/revoke", data=other).status_code == 200
    assert whoami(gateway, tokens["access_token"]).status_code == 200
    revoked = at_auth(
        gateway, "POST", "/revoke", data={"token": tokens["refresh_token"], "client_id": APP}
    )
    assert revoked.status_code == 200
    ended = whoami(gateway, tokens["access_token"])
    assert ended.status_code == 401
    assert "sign-in was ended" in ended.json()["error_description"]

    # Revoking the access token itself ends the whole sign-in: the refresh token too.
    tokens = signed_in(resource=IDENTITY)
    assert call(gateway, "debitmydata_get_profile", tokens["access_token"]).status_code == 200
    by_access = {"token": tokens["access_token"], "client_id": APP}
    assert at_auth(gateway, "POST", "/revoke", data=by_access).status_code == 200
    assert call(gateway, "debitmydata_get_profile", tokens["access_token"]).status_code == 401
    after = token(gateway, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert after.json()["error"] == "invalid_grant"

    # A copied refresh token used twice: everything from that sign-in stops.
    tokens = signed_in()
    newer = token(gateway, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert newer.status_code == 200
    replay = token(gateway, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert replay.json()["error"] == "invalid_grant"
    assert whoami(gateway, newer.json()["access_token"]).status_code == 401

    # Junk, or someone else's token, is no error (RFC 7009) and ends nothing.
    junk = {"token": "not-a-token", "client_id": APP}
    assert at_auth(gateway, "POST", "/revoke", data=junk).status_code == 200


def test_agents_tokens_for_the_identity_endpoint(gateway: TestClient) -> None:  # noqa: F811
    """An agent signs in with its own key for the identity endpoint too; its tokens carry no
    sign-in (nothing to end: they last 10 minutes, and stop when the DID-LLM does)."""
    did = issue(gateway)["did"]
    access = sign_in(gateway, did, resource=IDENTITY)
    public = keys.load_private_key(gateway.key_path).public_key()  # type: ignore[attr-defined]
    claims = verify_token(access, public, issuer=ISS, audiences=[IDENTITY], now=datetime.now(UTC))
    assert claims["client_id"] == did and "sid" not in claims
    assert result(call(gateway, "debitmydata_whoami", access))["signed_in_as"] == did
    # The tools' earlier names (before 0.50.0) still work here too, with the token only.
    assert result(call(gateway, "dmd_whoami", access))["signed_in_as"] == did
    assert result(call(gateway, "dmd_get_profile", access))["did"] == did
    assert call(gateway, "dmd_whoami", None).status_code == 401


def test_no_identity_endpoint_while_sign_in_is_closed(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    with _app_client(ready_registry, engine, issuer_key=ISSUER) as client:
        listed = rpc(client, "tools/list", {}, path=IDENTITY_PATH)
        assert listed.status_code == 404
        assert client.get(METADATA, headers=HOST).status_code == 404
        assert client.app.state.identity_mcp is None  # type: ignore[attr-defined]


def test_googles_domain_verification_file(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    path = "/.well-known/google-connected-apps"
    with _app_client(ready_registry, engine, issuer_key=ISSUER) as client:
        assert client.get(path).status_code == 404  # not set
    proof = tmp_path / "google-connected-apps.json"
    overrides = {"google_connected_apps_file": proof}
    with _app_client(
        ready_registry, engine, issuer_key=ISSUER, settings_overrides=overrides
    ) as client:
        assert client.get(path).status_code == 404  # set, but no file yet
        proof.write_text('{"challenge": "abc123"}')
        answer = client.get(path, follow_redirects=False)
        assert answer.status_code == 200 and answer.json() == {"challenge": "abc123"}
        assert answer.headers["content-type"] == "application/json"
        proof.write_bytes(os.urandom(70 * 1024))  # never more than a small file
        assert client.get(path).status_code == 404


def test_whoami_answers_what_the_whoami_screen_shows(gateway: TestClient) -> None:  # noqa: F811
    """WhoAmI's answer as the screen agreed for it uses it: the DID's suffix to show and the full
    DID to copy (never the internal ID), the DID-LLM's status apart from the session (status,
    access in words, its end with timezone), internal details in advanced; never cached."""
    issued = issue(gateway)
    did = issued["did"]
    verifier, challenge_value = pkce()
    code = code_for(gateway, did, challenge_value, resource=IDENTITY)
    exchanged = token(
        gateway,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
        resource=IDENTITY,
    )
    assert exchanged.status_code == 200, exchanged.text
    response = call(gateway, "debitmydata_whoami", exchanged.json()["access_token"])
    assert response.headers["cache-control"] == "no-store"
    answer = result(response)

    assert answer["did"] == did  # Copy ID: the full DID, exactly
    assert answer["did_llm_id"] == did.rsplit(":", 1)[-1]
    assert did.endswith(f":agents:{answer['did_llm_id']}")
    assert not answer["did_llm_id"].startswith("dmd_")
    assert answer["identity_status"] == "active"
    assert answer["display_name"] and answer["participant_type"]
    assert isinstance(answer["entitlement"], list)
    assert "controller_wallet" in answer  # null: the screen shows "Not linked"
    assert datetime.fromisoformat(answer["registered_at"]) <= datetime.now(UTC)
    assert answer["environment"] == gateway.app.state.settings.environment  # type: ignore[attr-defined]

    session = answer["session"]
    assert session["status"] == "active" and session["kind"] == "app"
    assert session["access"] == ["Read your own DID-LLM's records"]
    assert session["timezone"] == "UTC"
    ends = datetime.fromisoformat(session["expires_at"])
    assert ends.utcoffset() is not None  # with its timezone
    days = (ends - datetime.now(UTC)).days
    assert 28 <= days <= 30  # the sign-in's end, not the 10-minute access token's

    advanced = answer["advanced"]
    assert advanced["identity_id"] == issued["identity_id"]
    assert advanced["identity_id"].startswith("dmd_identity_")
    assert advanced["did_document_url"].endswith("/did.json")
    assert all(url.startswith("http") for url in advanced["credentials"])
    assert "dmd_identity_" not in json.dumps(
        {key: value for key, value in answer.items() if key in SCREEN}
    )
    # The answer before 0.55.15 is still there, for the apps written for it.
    assert answer["signed_in_as"] == did and answer["identity"]["did"] == did

    # An agent's sign-in is its access token alone: it ends with it.
    agent = result(call(gateway, "debitmydata_whoami", sign_in(gateway, did, resource=IDENTITY)))
    assert agent["session"]["kind"] == "agent"
    assert agent["session"]["expires_at"] == agent["advanced"]["access_token_expires_at"]

    # Nothing from the MCP endpoints is kept by a shared cache, signed in or not.
    listed = rpc(gateway, "tools/list", {})
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"


SCREEN = (
    "did",
    "did_llm_id",
    "display_name",
    "participant_type",
    "identity_status",
    "entitlement",
    "controller_wallet",
    "registered_at",
    "environment",
    "session",
)
