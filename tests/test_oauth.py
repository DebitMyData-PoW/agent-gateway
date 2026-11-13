"""OAuth phase 1: the authorization server at the auth host (api/oauth.py, oauth/).

An agent signs a short JWT with its DID-LLM's own key and gets an access token for the MCP
server. Covered: the keys, the signed request's checks, the access tokens, the settings, and
against PostgreSQL the token endpoint end to end, including every refusal.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, text

from dmd_gateway.oauth import keys
from dmd_gateway.oauth.assertion import (
    ASSERTION_TYPE,
    AssertionRejected,
    verify_assertion,
)
from dmd_gateway.oauth.tokens import TokenRejected, issue_token, verify_token
from dmd_gateway.settings import Settings

from .conftest import TEST_DATABASE_URL
from .fakes import AGENT_CONTROLLER, AGENT_KEY
from .test_identity_status import issue, run
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import ISSUER, clean_chain, engine  # noqa: F401

AUTH = "auth.localhost.test"
ISS = f"https://{AUTH}"
TOKEN_URL = f"{ISS}/token"
MCP = "http://localhost:8000/agenticsuperhighway"
DID = "did:web:localhost%3A8000:agents:test"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
# One RSA key for the whole module: 3072-bit keys take a moment to make.
RSA_KEY = generate_private_key(public_exponent=65537, key_size=2048)

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def signed(
    did: str = DID,
    *,
    key: Ed25519PrivateKey = AGENT_KEY,
    aud: str = TOKEN_URL,
    now: datetime = NOW,
    lifetime: int = 60,
    algorithm: str = "EdDSA",
    **changes: Any,
) -> str:
    """What the agent sends: a JWT about itself, signed with its DID-LLM's key."""
    issued = int(now.timestamp())
    claims = {
        "iss": did,
        "sub": did,
        "aud": aud,
        "iat": issued,
        "exp": issued + lifetime,
        "jti": secrets.token_urlsafe(16),
        **changes,
    }
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, key, algorithm=algorithm)


def lookup(did: str) -> str | AssertionRejected:
    return AGENT_CONTROLLER if did == DID else AssertionRejected("unknown DID-LLM")


def check(token: str, now: datetime = NOW) -> Any:
    return verify_assertion(token, audience=[TOKEN_URL, ISS], controller_key=lookup, now=now)


# --------------------------------------------------------------------------- keys


def test_new_key_file_is_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "oauth.pem"
    jwk = keys.write_new_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert jwk["kty"] == "RSA" and jwk["alg"] == "RS256" and jwk["use"] == "sig"
    loaded = keys.load_private_key(path)
    assert loaded.key_size == keys.KEY_SIZE
    assert keys.public_jwk(loaded.public_key()) == jwk
    with pytest.raises(FileExistsError):
        keys.write_new_key(path)
    assert keys.main([str(path)]) == 1


def test_key_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "oauth.pem"
    keys.write_new_key(path)
    os.chmod(path, 0o644)
    with pytest.raises(keys.OAuthKeyError, match="chmod 600"):
        keys.load_private_key(path)
    with pytest.raises(keys.OAuthKeyError, match="not found"):
        keys.load_private_key(tmp_path / "missing.pem")


def test_jwk_round_trip_and_stable_kid() -> None:
    jwk = keys.public_jwk(RSA_KEY.public_key())
    assert jwk == keys.public_jwk(RSA_KEY.public_key())
    assert set(jwk) == {"kty", "n", "e", "kid", "use", "alg"}
    again = keys.load_public_jwk(json.dumps(jwk))
    assert again.public_numbers() == RSA_KEY.public_key().public_numbers()
    assert keys.jwk_set(RSA_KEY.public_key()) == {"keys": [jwk]}
    with pytest.raises(keys.OAuthKeyError):
        keys.load_public_jwk('{"kty": "EC"}')


# --------------------------------------------------------------------------- signed requests


def test_signed_request_accepted() -> None:
    token = signed()
    checked = check(token)
    assert checked.did == DID
    assert checked.expires_at == NOW + timedelta(seconds=60)
    assert checked.jti == jwt.decode(token, options={"verify_signature": False})["jti"]
    # The issuer itself is an accepted audience too (RFC 7523 allows either).
    assert check(signed(aud=ISS)).did == DID


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        ("not-a-jwt", "not a JWT"),
        (signed(key=Ed25519PrivateKey.generate()), "not the DID-LLM controller's"),
        (signed(aud="https://elsewhere.example/token"), "aud must be"),
        (signed(lifetime=301), "5 minutes at most"),
        (signed(lifetime=0), "5 minutes at most"),
        (signed(now=NOW - timedelta(minutes=5)), "expired"),
        (signed(now=NOW + timedelta(minutes=5)), "in the future"),
        (signed(sub="did:web:someone-else"), "sub must be"),
        (signed(iss="https://not-a-did.example"), "iss must be"),
        (signed(did="did:web:unknown.example"), "unknown DID-LLM"),
        (signed(jti="x" * 129), "jti must be"),
        (signed(jti=None), "jti"),
        (signed(nbf=int((NOW + timedelta(minutes=5)).timestamp())), "not valid yet"),
    ],
)
def test_signed_request_refused(token: str, reason: str) -> None:
    with pytest.raises(AssertionRejected) as refused:
        check(token)
    assert reason in refused.value.reason


def test_only_eddsa_is_accepted() -> None:
    for algorithm, key in (("HS256", "a shared secret of 32 bytes or more!"), ("RS256", RSA_KEY)):
        token = signed(key=key, algorithm=algorithm)  # type: ignore[arg-type]
        with pytest.raises(AssertionRejected, match="EdDSA"):
            check(token)
    unsigned = jwt.encode({"iss": DID, "sub": DID}, None, algorithm="none")
    with pytest.raises(AssertionRejected, match="EdDSA"):
        check(unsigned)


# --------------------------------------------------------------------------- access tokens


def test_access_token_round_trip() -> None:
    issued = issue_token(
        RSA_KEY, issuer=ISS, did=DID, audience=MCP, scopes=("did:read",), ttl_seconds=600, now=NOW
    )
    assert issued.expires_in == 600 and issued.scope == "did:read"
    header = jwt.get_unverified_header(issued.access_token)
    assert header["typ"] == "at+jwt" and header["alg"] == "RS256"
    assert header["kid"] == keys.public_jwk(RSA_KEY.public_key())["kid"]
    claims = verify_token(
        issued.access_token, RSA_KEY.public_key(), issuer=ISS, audiences=[MCP], now=NOW
    )
    assert claims["sub"] == claims["client_id"] == DID
    assert claims["aud"] == MCP and claims["jti"] == issued.jti
    assert claims["exp"] - claims["iat"] == 600


def test_access_token_refusals() -> None:
    issued = issue_token(
        RSA_KEY, issuer=ISS, did=DID, audience=MCP, scopes=("did:read",), ttl_seconds=600, now=NOW
    )
    public = RSA_KEY.public_key()
    with pytest.raises(TokenRejected):  # for another resource
        verify_token(
            issued.access_token, public, issuer=ISS, audiences=["https://x.example"], now=NOW
        )
    with pytest.raises(TokenRejected):  # from another issuer
        verify_token(
            issued.access_token, public, issuer="https://x.example", audiences=[MCP], now=NOW
        )
    with pytest.raises(TokenRejected, match="expired"):
        verify_token(
            issued.access_token, public, issuer=ISS, audiences=[MCP], now=NOW + timedelta(hours=1)
        )
    other = generate_private_key(public_exponent=65537, key_size=2048).public_key()
    with pytest.raises(TokenRejected):  # signed by another key
        verify_token(issued.access_token, other, issuer=ISS, audiences=[MCP], now=NOW)
    # The agent's own signed request is not an access token.
    with pytest.raises(TokenRejected, match="not an access token"):
        verify_token(signed(), public, issuer=ISS, audiences=[MCP], now=NOW)


# --------------------------------------------------------------------------- settings


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, database_url="postgresql+psycopg://x@localhost/x", **values)


def test_settings() -> None:
    assert _settings(auth_host=" Auth.DebitMyData.io ").auth_host == "auth.debitmydata.io"
    with pytest.raises(ValidationError, match="host names only"):
        _settings(auth_host="https://auth.debitmydata.io")
    with pytest.raises(ValidationError, match="host name of its own"):
        _settings(auth_host="id.debitmydata.io", app_host="id.debitmydata.io")
    with pytest.raises(ValidationError, match="host name of its own"):
        _settings(auth_host="api.debitmydata.io", public_base_url="https://api.debitmydata.io")
    with pytest.raises(ValidationError, match="signer service"):
        _settings(
            environment="production",
            public_base_url="https://api.debitmydata.io",
            oauth_key_path="/etc/dmd/oauth.pem",
        )
    with pytest.raises(ValidationError):
        _settings(oauth_token_ttl_seconds=7200)


# --------------------------------------------------------------------------- the server


@pytest.fixture
def key_path(tmp_path: Path) -> Path:
    path = tmp_path / "oauth.pem"
    keys.write_new_key(path)
    return path


@pytest.fixture
def auth(ready_registry: Path, engine: Engine, key_path: Path) -> Iterator[TestClient]:  # noqa: F811
    overrides = {"auth_host": AUTH, "oauth_key_path": key_path}
    with _app_client(
        ready_registry, engine, issuer_key=ISSUER, settings_overrides=overrides
    ) as test_client:
        yield test_client


def at_auth(client: TestClient, method: str, path: str, **kwargs: Any) -> Any:
    headers = {"host": AUTH, **kwargs.pop("headers", {})}
    return client.request(method, path, headers=headers, **kwargs)


def token_request(client: TestClient, assertion: str, **fields: str) -> Any:
    form = {
        "grant_type": "client_credentials",
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": assertion,
        **fields,
    }
    return at_auth(client, "POST", "/token", data=form)


def live(did: str, **changes: Any) -> str:
    """A signed request for the test client's clock (the server checks against real time)."""
    return signed(did, now=datetime.now(UTC), **changes)


@needs_db
def test_metadata_and_keys(auth: TestClient, key_path: Path) -> None:
    response = at_auth(auth, "GET", "/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    meta = response.json()
    assert meta["issuer"] == ISS
    assert meta["token_endpoint"] == TOKEN_URL
    assert meta["jwks_uri"] == f"{ISS}/.well-known/jwks.json"
    assert meta["grant_types_supported"] == ["client_credentials"]
    assert meta["token_endpoint_auth_methods_supported"] == ["private_key_jwt"]
    assert meta["token_endpoint_auth_signing_alg_values_supported"] == ["EdDSA"]
    assert meta["scopes_supported"] == ["did:read"]
    jwks = at_auth(auth, "GET", "/.well-known/jwks.json").json()
    public = keys.load_private_key(key_path).public_key()
    assert jwks == {"keys": [keys.public_jwk(public)]}
    assert "d" not in jwks["keys"][0]  # never the private part


@needs_db
def test_auth_host_shows_nothing_else(auth: TestClient) -> None:
    for path in ("/", "/healthz", "/v1/discovery", "/get-did", "/llms.txt", "/agenticsuperhighway"):
        assert at_auth(auth, "GET", path).status_code == 404, path
    # ... and the API's own address does not show the authorization server.
    assert auth.get("/.well-known/oauth-authorization-server").status_code == 404
    assert auth.get("/.well-known/jwks.json").status_code == 404
    assert auth.post("/token", data={"grant_type": "client_credentials"}).status_code == 404
    assert auth.get("/healthz").status_code == 200


@needs_db
def test_no_key_no_authorization_server(ready_registry: Path, engine: Engine) -> None:  # noqa: F811
    with _app_client(
        ready_registry, engine, issuer_key=ISSUER, settings_overrides={"auth_host": AUTH}
    ) as client:
        assert at_auth(client, "GET", "/.well-known/oauth-authorization-server").status_code == 404
        assert at_auth(client, "GET", "/.well-known/jwks.json").status_code == 404
        assert token_request(client, signed()).status_code == 404


@needs_db
def test_token_issued_once_per_signed_request(
    auth: TestClient,
    engine: Engine,  # noqa: F811
    key_path: Path,
) -> None:
    did = issue(auth)["did"]
    assertion = live(did)
    response = token_request(auth, assertion, resource=MCP, scope="did:read", client_id=did)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 600
    assert body["scope"] == "did:read"
    public = keys.load_private_key(key_path).public_key()
    claims = verify_token(
        body["access_token"], public, issuer=ISS, audiences=[MCP], now=datetime.now(UTC)
    )
    assert claims["sub"] == did and claims["aud"] == MCP
    with engine.connect() as conn:
        event = conn.execute(
            text(
                "SELECT actor_type, actor_id, data FROM audit_events"
                " WHERE action = 'oauth.token_issued' ORDER BY id DESC LIMIT 1"
            )
        ).one()
    assert event.actor_type == "agent" and event.actor_id == did
    assert event.data["token_id"] == claims["jti"]
    assert "access_token" not in event.data  # the token itself is never kept

    replay = token_request(auth, assertion)
    assert replay.status_code == 401
    assert replay.json()["error"] == "invalid_client"
    assert "already used" in replay.json()["error_description"]

    # Defaults: the MCP server, did:read. The old /mcp address is accepted as a resource too.
    plain = token_request(auth, live(did))
    assert plain.status_code == 200, plain.text
    old = token_request(auth, live(did), resource="http://localhost:8000/mcp")
    assert old.status_code == 200, old.text
    assert jwt.decode(old.json()["access_token"], options={"verify_signature": False})["aud"] == (
        "http://localhost:8000/mcp"
    )


@needs_db
def test_token_refusals(auth: TestClient) -> None:
    did = issue(auth)["did"]

    def refused(response: Any, status: int, error: str, words: str = "") -> None:
        assert response.status_code == status, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["error"] == error
        assert words in body["error_description"]

    refused(token_request(auth, live(did), scope="did:write"), 400, "invalid_scope", "did:write")
    refused(
        token_request(auth, live(did), resource="https://elsewhere.example/mcp"),
        400,
        "invalid_target",
    )
    refused(
        token_request(auth, live(did), grant_type="authorization_code"),
        400,
        "unsupported_grant_type",
    )
    refused(
        token_request(auth, live(did), client_assertion_type="jwt"),
        400,
        "invalid_request",
        ASSERTION_TYPE,
    )
    refused(token_request(auth, live(did), client_id="did:web:other"), 401, "invalid_client")
    refused(token_request(auth, live("did:web:unknown.example")), 401, "invalid_client", "unknown")
    refused(
        token_request(auth, signed(did, now=datetime.now(UTC) - timedelta(minutes=10))),
        401,
        "invalid_client",
        "expired",
    )
    refused(
        token_request(auth, live(did, key=Ed25519PrivateKey.generate())),
        401,
        "invalid_client",
        "signature",
    )
    as_json = at_auth(
        auth,
        "POST",
        "/token",
        json={"grant_type": "client_credentials", "client_assertion": live(did)},
    )
    refused(as_json, 400, "invalid_request", "x-www-form-urlencoded")
    twice = at_auth(
        auth,
        "POST",
        "/token",
        content=f"grant_type=client_credentials&grant_type=client_credentials"
        f"&client_assertion_type={ASSERTION_TYPE}&client_assertion={live(did)}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    refused(twice, 400, "invalid_request", "more than once")
    huge = at_auth(
        auth,
        "POST",
        "/token",
        content="a=" + "x" * 9000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    refused(huge, 400, "invalid_request", "larger than")


@needs_db
def test_suspended_and_wallet_did_cannot_sign_in(auth: TestClient, engine: Engine) -> None:  # noqa: F811
    issued = issue(auth)
    did = issued["did"]
    code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code == 0
    response = token_request(auth, live(did))
    assert response.status_code == 401
    assert "suspended" in response.json()["error_description"]

    other = issue(auth)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE identities SET controller_public_key = NULL,"
                " controller_wallet_address = :wallet WHERE id = :id"
            ),
            {"wallet": "0x" + "ab" * 20, "id": other["identity_id"]},
        )
    response = token_request(auth, live(other["did"]))
    assert response.status_code == 401
    assert "wallet" in response.json()["error_description"]
