"""OAuth phase 2: people sign in to apps (MCP clients) with their DID-LLM in the browser
(api/oauth_browser.py, services/oauth_sign_in.py, oauth/clients.py).

Covers: the apps' Client ID Metadata Documents and how they are fetched; the authorization
request and every refusal; signing in with the DID-LLM's key (and with the owner-key tool) or
its wallet; the code exchange with PKCE; refresh tokens (rotation, replay); revocation; apps
that sign their token requests; and that it stays closed where the signer service signs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from eth_account import Account
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from dmd_gateway.identity.wallets import WalletVerifier
from dmd_gateway.oauth import keys
from dmd_gateway.oauth.assertion import ASSERTION_TYPE
from dmd_gateway.oauth.clients import (
    ClientDirectory,
    ClientRejected,
    check_client_id,
    parse_document,
    redirect_allowed,
    safe_fetch,
)
from dmd_gateway.oauth.tokens import verify_token

from .conftest import TEST_DATABASE_URL
from .fakes import AGENT_KEY, sign_challenge
from .test_identity_status import issue, run
from .test_mcp_auth import _through, whoami
from .test_oauth import AUTH, ISS, MCP
from .test_owner_key_tool import _tool
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import (  # noqa: F401
    ISSUER,
    answer,
    challenge,
    clean_chain,
    engine,
    quote,
    register,
    settle,
)
from .test_wallet_registration import wallet_signature

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

APP = "https://app.example.com/oauth/client.json"
CALLBACK = "https://app.example.com/callback"
SIGNED_APP = "https://signed.example.com/client.json"
SIGNED_CALLBACK = "https://signed.example.com/cb"
APP_KEY = generate_private_key(public_exponent=65537, key_size=2048)
APP_JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(APP_KEY.public_key())), "kid": "k1"}
DOCUMENTS: dict[str, dict[str, Any]] = {
    APP: {
        "client_id": APP,
        "client_name": "Example   App",
        "redirect_uris": [CALLBACK, "http://127.0.0.1:33418/callback", "ftp://nope"],
        "token_endpoint_auth_method": "none",
    },
    SIGNED_APP: {
        "client_id": SIGNED_APP,
        "client_name": "Signed App",
        "redirect_uris": [SIGNED_CALLBACK],
        "token_endpoint_auth_methods_supported": ["private_key_jwt"],
        "jwks": {"keys": [APP_JWK]},
    },
}


def fake_fetch(url: str) -> tuple[bytes, Mapping[str, str]]:
    if url not in DOCUMENTS:
        raise ClientRejected(f"{url} answered HTTP 404")
    return json.dumps(DOCUMENTS[url]).encode(), {"Cache-Control": "max-age=600"}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# --------------------------------------------------------------------------- the apps


def test_client_ids_are_https_documents() -> None:
    assert check_client_id(APP) == "app.example.com"
    for bad in (
        "http://app.example.com/client.json",
        "https://app.example.com",
        "https://app.example.com/",
        "https://app.example.com/c.json?x=1",
        "https://app.example.com/c.json#x",
        "https://user:pw@app.example.com/c.json",
        "https://app.example.com:8443/c.json",
        "https://localhost/c.json",
        "did:web:app.example.com",
    ):
        with pytest.raises(ClientRejected):
            check_client_id(bad)


def test_metadata_documents() -> None:
    client = parse_document(APP, json.dumps(DOCUMENTS[APP]).encode())
    assert client.name == "Example App" and client.host == "app.example.com"
    assert client.redirect_uris == (CALLBACK, "http://127.0.0.1:33418/callback")
    assert client.auth_methods == {"none"}
    signed = parse_document(SIGNED_APP, json.dumps(DOCUMENTS[SIGNED_APP]).encode())
    assert signed.auth_methods == {"private_key_jwt"} and signed.jwks is not None
    for url, document, reason in (
        (APP, {**DOCUMENTS[APP], "client_id": SIGNED_APP}, "another client_id"),
        (APP, {**DOCUMENTS[APP], "client_name": " "}, "client_name"),
        (APP, {**DOCUMENTS[APP], "redirect_uris": ["http://app.example.com/cb"]}, "https"),
        (APP, {**DOCUMENTS[APP], "redirect_uris": []}, "redirect_uris"),
        (SIGNED_APP, {**DOCUMENTS[SIGNED_APP], "jwks": None}, "publishes no keys"),
    ):
        with pytest.raises(ClientRejected, match=reason):
            parse_document(url, json.dumps(document).encode())
    with pytest.raises(ClientRejected, match="not JSON"):
        parse_document(APP, b"<html>")


def test_documents_are_fetched_once_then_kept() -> None:
    calls: list[str] = []
    now = [0.0]

    def fetch(url: str) -> tuple[bytes, Mapping[str, str]]:
        calls.append(url)
        return fake_fetch(url)

    directory = ClientDirectory(fetch=fetch, clock=lambda: now[0])
    assert directory.get(APP).name == "Example App"
    directory.get(APP)
    assert calls == [APP]
    now[0] = 601.0
    directory.get(APP)
    assert calls == [APP, APP]
    with pytest.raises(ClientRejected, match="404"):
        directory.get("https://unknown.example.com/c.json")


@pytest.mark.parametrize(
    "address", ["10.0.0.7", "127.0.0.1", "169.254.169.254", "192.168.1.2", "::1", "fd00::1"]
)
def test_documents_are_never_fetched_from_private_addresses(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolve(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(ClientRejected, match="not a public site"):
        safe_fetch("https://rebind.example.com/client.json")


# --------------------------------------------------------------------------- the server


@pytest.fixture
def gateway(ready_registry: Path, engine: Engine, tmp_path: Path) -> Iterator[TestClient]:  # noqa: F811
    key_path = tmp_path / "oauth.pem"
    keys.write_new_key(key_path)
    overrides = {"auth_host": AUTH, "oauth_key_path": key_path}
    with _app_client(
        ready_registry, engine, issuer_key=ISSUER, settings_overrides=overrides
    ) as client:
        client.app.state.oauth_clients = ClientDirectory(fetch=fake_fetch)  # type: ignore[attr-defined]
        client.app.state.wallet_verifier = WalletVerifier({})  # type: ignore[attr-defined]
        client.key_path = key_path  # type: ignore[attr-defined]
        yield client


def at_auth(client: TestClient, method: str, path: str, **kwargs: Any) -> Any:
    headers = {"host": AUTH, **kwargs.pop("headers", {})}
    return client.request(method, path, headers=headers, follow_redirects=False, **kwargs)


def authorize(client: TestClient, challenge_value: str, **changes: Any) -> Any:
    params = {
        "response_type": "code",
        "client_id": APP,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge_value,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "did:read",
        "resource": MCP,
        **changes,
    }
    return at_auth(
        client, "GET", "/authorize", params={k: v for k, v in params.items() if v is not None}
    )


def answer_of(location: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


def start(client: TestClient, challenge_value: str, **changes: Any) -> str:
    response = authorize(client, challenge_value, **changes)
    assert response.status_code == 303, response.text
    page = response.headers["location"]
    assert page.startswith("/authorize/dmd_signin_")
    return page


def code_for(client: TestClient, did: str, challenge_value: str, **changes: Any) -> str:
    page = start(client, challenge_value, **changes)
    chosen = at_auth(client, "POST", f"{page}/did", json={"did": did})
    assert chosen.status_code == 200, chosen.text
    signed = at_auth(
        client,
        "POST",
        f"{page}/signature",
        json={"signature": sign_challenge(chosen.json()["message"])},
    )
    assert signed.status_code == 200, signed.text
    finished = at_auth(client, "POST", f"{page}/finish")
    assert finished.status_code == 200, finished.text
    back = answer_of(finished.json()["redirect"])
    assert finished.json()["redirect"].startswith(CALLBACK + "?")
    assert back["state"] == "xyz" and back["iss"] == ISS
    return back["code"]


def token(client: TestClient, **fields: str) -> Any:
    return at_auth(client, "POST", "/token", data={"client_id": APP, **fields})


@needs_db
def test_sign_in_with_the_key_then_refresh_and_revoke(gateway: TestClient) -> None:
    did = issue(gateway)["did"]
    verifier, challenge_value = pkce()
    page = start(gateway, challenge_value)
    html = at_auth(gateway, "GET", page)
    assert html.status_code == 200
    assert "Example App (app.example.com)" in html.text
    assert html.headers["x-frame-options"] == "DENY"
    assert "authorization code flow with PKCE" in gateway.get("/llms.txt").text
    assert "frame-ancestors 'none'" in html.headers["content-security-policy"]
    assert "read your own DID-LLM" in html.text

    chosen = at_auth(gateway, "POST", f"{page}/did", json={"did": did}).json()
    assert chosen["controller"]["type"] == "key" and chosen["controllers"] == ["key"]
    lines = chosen["message"].split("\n")
    assert lines[:3] == [
        "DebitMyData sign-in",
        "Sign in to: Example App (app.example.com)",
        f"DID-LLM: {did}",
    ]
    assert f"Sign-in server: {ISS}" in lines and f"For: {MCP}" in lines
    # Not signed yet: no code.
    early = at_auth(gateway, "POST", f"{page}/finish")
    assert (early.status_code, early.json()["error"]["code"]) == (409, "sign_in_open")
    wrong = at_auth(
        gateway, "POST", f"{page}/signature", json={"signature": sign_challenge("other")}
    )
    assert wrong.status_code == 422 and "4 tries left" in wrong.json()["error"]["message"]
    ok = at_auth(
        gateway, "POST", f"{page}/signature", json={"signature": sign_challenge(chosen["message"])}
    )
    assert ok.json() == {"status": "approved"}
    code = answer_of(at_auth(gateway, "POST", f"{page}/finish").json()["redirect"])["code"]
    again = at_auth(gateway, "POST", f"{page}/finish")
    assert again.json()["error"]["code"] == "sign_in_finished"

    exchanged = token(
        gateway,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
        resource=MCP,
    )
    assert exchanged.status_code == 200, exchanged.text
    tokens = exchanged.json()
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "did:read"
    public = keys.load_private_key(gateway.key_path).public_key()  # type: ignore[attr-defined]
    claims = verify_token(
        tokens["access_token"], public, issuer=ISS, audiences=[MCP], now=datetime.now(UTC)
    )
    assert claims["sub"] == did and claims["client_id"] == APP
    me = whoami(gateway, tokens["access_token"]).json()["result"]["structuredContent"]
    assert me["signed_in_as"] == did

    refreshed = token(gateway, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text
    newer = refreshed.json()
    assert newer["refresh_token"] != tokens["refresh_token"]
    # The old refresh token again: someone copied it, so the whole sign-in ends.
    replay = token(gateway, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert (replay.status_code, replay.json()["error"]) == (400, "invalid_grant")
    ended = token(gateway, grant_type="refresh_token", refresh_token=newer["refresh_token"])
    assert ended.json()["error"] == "invalid_grant" and "ended" in ended.json()["error_description"]

    # A fresh sign-in, ended by the app.
    verifier, challenge_value = pkce()
    code = code_for(gateway, did, challenge_value)
    tokens = token(
        gateway,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
    ).json()
    revoked = at_auth(
        gateway, "POST", "/revoke", data={"token": tokens["refresh_token"], "client_id": APP}
    )
    assert revoked.status_code == 200
    after = token(gateway, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert after.json()["error"] == "invalid_grant"


@needs_db
def test_the_code_is_bound_to_the_app_and_used_once(gateway: TestClient) -> None:
    did = issue(gateway)["did"]
    verifier, challenge_value = pkce()
    code = code_for(gateway, did, challenge_value)
    good = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK}
    for fields, error in (
        ({**good, "code_verifier": pkce()[0]}, "invalid_grant"),
        (
            {**good, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:33418/callback"},
            "invalid_grant",
        ),
        (
            {**good, "code_verifier": verifier, "resource": "http://localhost:8000/other"},
            "invalid_target",
        ),
        ({**good, "code_verifier": verifier, "code": "nope"}, "invalid_grant"),
    ):
        refused = token(gateway, **fields)
        assert (refused.status_code, refused.json()["error"]) == (400, error), fields
    other_app = at_auth(
        gateway, "POST", "/token", data={**good, "code_verifier": verifier, "client_id": SIGNED_APP}
    )
    assert other_app.status_code == 401  # that app must sign its requests
    first = token(gateway, **good, code_verifier=verifier)
    assert first.status_code == 200, first.text
    # The code again: refused, and the tokens it gave stop refreshing (RFC 9700).
    second = token(gateway, **good, code_verifier=verifier)
    assert second.json()["error"] == "invalid_grant"
    dead = token(gateway, grant_type="refresh_token", refresh_token=first.json()["refresh_token"])
    assert dead.json()["error"] == "invalid_grant"


@needs_db
def test_authorization_requests_that_are_refused(gateway: TestClient) -> None:
    _, challenge_value = pkce()
    # Not sent anywhere: an unknown app, or a return address the app does not list.
    for changes in (
        {"client_id": "https://unknown.example.com/c.json"},
        {"client_id": "not-a-url"},
        {"redirect_uri": "https://evil.example.com/cb"},
        {"redirect_uri": "ftp://nope"},
    ):
        response = authorize(gateway, challenge_value, **changes)
        assert response.status_code == 400 and "cannot continue" in response.text, changes
    # Sent back to the app, with the error, its state and iss.
    for changes, error in (
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"scope": "did:read did:write"}, "invalid_scope"),
        ({"resource": "https://elsewhere.example/mcp"}, "invalid_target"),
    ):
        response = authorize(gateway, challenge_value, **changes)
        assert response.status_code == 303, changes
        back = answer_of(response.headers["location"])
        assert back["error"] == error and back["state"] == "xyz" and back["iss"] == ISS, changes
        assert response.headers["location"].startswith(CALLBACK)
    # A person who does not sign in.
    page = start(gateway, challenge_value)
    denied = at_auth(gateway, "POST", f"{page}/deny").json()
    assert answer_of(denied["redirect"])["error"] == "access_denied"
    assert at_auth(gateway, "GET", page).status_code == 409
    # Unknown DID-LLMs and sign-ins.
    page = start(gateway, challenge_value)
    unknown = at_auth(gateway, "POST", f"{page}/did", json={"did": "did:web:x.example:agents:y"})
    assert unknown.json()["error"]["code"] == "unknown_did"
    gone = at_auth(gateway, "GET", "/authorize/dmd_signin_01M76DH1YR57AT7ED8QGN6WR1B/request")
    assert gone.status_code == 404


@needs_db
def test_a_wallet_owner_signs_in_with_the_wallet(gateway: TestClient) -> None:
    owner = Account.create()
    q = quote(gateway, controller_public_key=None, controller_wallet_address=owner.address)
    rid = q["registration_intent_id"]
    c = challenge(gateway, rid)
    assert (
        answer(gateway, rid, c["challenge_id"], wallet_signature(owner, c["message"])).status_code
        == 201
    )
    settle(gateway, q, txid=secrets.token_hex(32))
    did = register(gateway, rid).json()["did"]

    _, challenge_value = pkce()
    page = start(gateway, challenge_value)
    chosen = at_auth(gateway, "POST", f"{page}/did", json={"did": did}).json()
    assert chosen["controller"]["type"] == "wallet"
    assert chosen["controller"]["value"].endswith(owner.address)
    other = Account.create()
    for tries_left in (4, 3, 2, 1, 0):
        wrong = at_auth(
            gateway,
            "POST",
            f"{page}/signature",
            json={"signature": wallet_signature(other, chosen["message"])},
        )
        assert wrong.status_code == 422
        hint = f"{tries_left} tries left" if tries_left else "start again from the app"
        assert hint in wrong.json()["error"]["message"]
    blocked = at_auth(
        gateway,
        "POST",
        f"{page}/signature",
        json={"signature": wallet_signature(owner, chosen["message"])},
    )
    assert blocked.status_code == 429

    page = start(gateway, challenge_value)
    chosen = at_auth(gateway, "POST", f"{page}/did", json={"did": did}).json()
    ok = at_auth(
        gateway,
        "POST",
        f"{page}/signature",
        json={"signature": wallet_signature(owner, chosen["message"])},
    )
    assert ok.status_code == 200, ok.text
    assert "code" in answer_of(at_auth(gateway, "POST", f"{page}/finish").json()["redirect"])


@needs_db
def test_suspended_did_llms_cannot_sign_in_or_refresh(gateway: TestClient, engine: Engine) -> None:  # noqa: F811
    did = issue(gateway)["did"]
    verifier, challenge_value = pkce()
    code = code_for(gateway, did, challenge_value)
    tokens = token(
        gateway,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
    ).json()
    code_, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code_ == 0
    refused = token(gateway, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert refused.json()["error"] == "invalid_grant" and "suspended" in refused.text
    page = start(gateway, challenge_value)
    chosen = at_auth(gateway, "POST", f"{page}/did", json={"did": did})
    assert chosen.json()["error"]["code"] == "identity_not_active"


@needs_db
def test_an_app_that_signs_its_token_requests(gateway: TestClient) -> None:
    did = issue(gateway)["did"]
    verifier, challenge_value = pkce()
    page = start(gateway, challenge_value, client_id=SIGNED_APP, redirect_uri=SIGNED_CALLBACK)
    chosen = at_auth(gateway, "POST", f"{page}/did", json={"did": did}).json()
    assert "Signed App (signed.example.com)" in chosen["message"]
    at_auth(
        gateway, "POST", f"{page}/signature", json={"signature": sign_challenge(chosen["message"])}
    )
    code = answer_of(at_auth(gateway, "POST", f"{page}/finish").json()["redirect"])["code"]

    def assertion(**changes: Any) -> str:
        now = int(datetime.now(UTC).timestamp())
        claims = {
            "iss": SIGNED_APP,
            "sub": SIGNED_APP,
            "aud": f"{ISS}/token",
            "exp": now + 60,
            "iat": now,
            "jti": secrets.token_urlsafe(12),
            **changes,
        }
        return jwt.encode(claims, APP_KEY, algorithm="RS256", headers={"kid": "k1"})

    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SIGNED_CALLBACK,
        "code_verifier": verifier,
        "client_id": SIGNED_APP,
    }
    unsigned = at_auth(gateway, "POST", "/token", data=fields)
    assert (unsigned.status_code, unsigned.json()["error"]) == (401, "invalid_client")
    forged_key = generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {
            "iss": SIGNED_APP,
            "sub": SIGNED_APP,
            "aud": f"{ISS}/token",
            "exp": 9999999999,
            "jti": "x",
        },
        forged_key,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    for bad in (forged, assertion(aud="https://elsewhere.example/token"), assertion(sub="x")):
        refused = at_auth(
            gateway,
            "POST",
            "/token",
            data={**fields, "client_assertion_type": ASSERTION_TYPE, "client_assertion": bad},
        )
        assert (refused.status_code, refused.json()["error"]) == (401, "invalid_client")
    signed = assertion()
    ok = at_auth(
        gateway,
        "POST",
        "/token",
        data={**fields, "client_assertion_type": ASSERTION_TYPE, "client_assertion": signed},
    )
    assert ok.status_code == 200, ok.text
    replay = at_auth(
        gateway,
        "POST",
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": ok.json()["refresh_token"],
            "client_id": SIGNED_APP,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": signed,
        },
    )
    assert replay.json()["error"] == "invalid_client" and "already used" in replay.text


@needs_db
def test_the_owner_key_tool_signs_in_from_the_page(
    gateway: TestClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(urllib.request, "urlopen", _through(gateway))
    verifier, challenge_value = pkce()
    page = start(gateway, challenge_value)
    address = f"{ISS}{page}"
    with pytest.raises(SystemExit, match="Enter your DID-LLM on the page first"):
        tool.approve(str(key_file), address, confirm=lambda _: "yes")
    at_auth(gateway, "POST", f"{page}/did", json={"did": did})
    with pytest.raises(SystemExit, match="Not signed"):
        tool.approve(str(key_file), address, confirm=lambda _: "no")
    tool.approve(str(key_file), address, confirm=lambda _: "yes")
    out = capsys.readouterr().out
    assert "Sign in to: Example App (app.example.com)" in out and "Signed in." in out
    code = answer_of(at_auth(gateway, "POST", f"{page}/finish").json()["redirect"])["code"]
    exchanged = token(
        gateway,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
    )
    assert exchanged.status_code == 200
    with pytest.raises(SystemExit, match="sign-in page's address"):
        tool.approve(str(key_file), "https://evil.example/authorize/x", confirm=lambda _: "yes")


@needs_db
def test_the_sign_in_server_shows_only_its_own_addresses(gateway: TestClient) -> None:
    _, challenge_value = pkce()
    assert at_auth(gateway, "GET", "/logo.png").status_code == 200
    assert at_auth(gateway, "GET", "/v1/discovery").status_code == 404
    assert at_auth(gateway, "GET", "/get-did").status_code == 404
    # ... and the API's host shows none of the sign-in.
    params = {"client_id": APP, "redirect_uri": CALLBACK, "code_challenge": challenge_value}
    assert gateway.get("/authorize", params=params, follow_redirects=False).status_code == 404
    assert gateway.post("/revoke", data={"token": "x", "client_id": APP}).status_code == 404


@needs_db
def test_browser_sign_in_stays_closed_where_the_signer_signs(gateway: TestClient) -> None:
    class SignerService:  # stands in for RemoteTokenSigner: not this process's key
        def __init__(self, key: Any) -> None:
            self.public_keys = (key.public_key(),)

    gateway.app.state.token_signer = SignerService(APP_KEY)  # type: ignore[attr-defined]
    meta = at_auth(gateway, "GET", "/.well-known/oauth-authorization-server").json()
    assert "authorization_endpoint" not in meta
    assert meta["grant_types_supported"] == ["client_credentials"]
    _, challenge_value = pkce()
    assert authorize(gateway, challenge_value).status_code == 404
    assert "authorization code flow" not in gateway.get("/llms.txt").text
    refused = token(gateway, grant_type="authorization_code", code="x", redirect_uri=CALLBACK)
    assert refused.json()["error"] == "unsupported_grant_type"


@needs_db
def test_expiry_reload_and_cleanup(gateway: TestClient, engine: Engine) -> None:  # noqa: F811
    did = issue(gateway)["did"]
    _, challenge_value = pkce()
    page = start(gateway, challenge_value)
    chosen = at_auth(gateway, "POST", f"{page}/did", json={"did": did}).json()
    at_auth(
        gateway, "POST", f"{page}/signature", json={"signature": sign_challenge(chosen["message"])}
    )
    # Signed with the tool, then the page reloaded: it still offers to continue.
    assert at_auth(gateway, "GET", page).status_code == 200
    assert at_auth(gateway, "GET", f"{page}/request").json()["status"] == "approved"
    sign_in_id = page.rsplit("/", 1)[1]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE oauth_sign_ins SET expires_at = now() - interval '2 days' WHERE id = :id"),
            {"id": sign_in_id},
        )
    late = at_auth(gateway, "POST", f"{page}/finish")
    assert (late.status_code, late.json()["error"]["code"]) == (410, "sign_in_expired")
    # Long-expired sign-ins are deleted when the next one starts.
    start(gateway, challenge_value)
    with engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM oauth_sign_ins WHERE id = :id"), {"id": sign_in_id}
        ).scalar()
    assert left == 0


def test_loopback_return_addresses_match_on_any_port() -> None:
    listed = (
        "https://claude.ai/api/mcp/auth_callback",
        "http://localhost/callback",
        "http://127.0.0.1/callback",
    )
    assert redirect_allowed("https://claude.ai/api/mcp/auth_callback", listed)
    assert redirect_allowed("http://localhost:3118/callback", listed)
    assert redirect_allowed("http://127.0.0.1:51234/callback", listed)
    for refused in (
        "https://claude.ai/api/mcp/other",
        "https://claude.ai:8443/api/mcp/auth_callback",
        "http://localhost:3118/other",
        "http://127.0.0.2:3118/callback",
        "http://localhost.evil.example/callback",
        "https://localhost:3118/callback",
    ):
        assert not redirect_allowed(refused, listed), refused


@needs_db
def test_the_page_names_where_the_person_returns(gateway: TestClient) -> None:
    _, challenge_value = pkce()
    page = start(gateway, challenge_value)
    assert "go back to <b>app.example.com</b>" in at_auth(gateway, "GET", page).text
    page = start(gateway, challenge_value, redirect_uri="http://127.0.0.1:50999/callback")
    text = at_auth(gateway, "GET", page).text
    assert "go back to <b>127.0.0.1</b>" in text and "an app on this computer" in text
