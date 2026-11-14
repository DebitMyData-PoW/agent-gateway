"""OAuth in production: the signer service signs the access tokens (signer/tokens.py), the API
holds only the public key (RemoteTokenSigner).

The signer runs here on a real Unix socket against the test database. Covers: its own checks
before it signs a token (whatever the API asks), sign-in end to end through it, and what
agents see when it is down, not set up, or signs with another key than the API expects.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from dmd_gateway.oauth import keys
from dmd_gateway.oauth.tokens import verify_token
from dmd_gateway.signer.client import SignerRefused, SignerUnavailable, call_signer
from dmd_gateway.signer.service import Signer, _token_issuer
from dmd_gateway.signer.tokens import TokenIssuer, TokenRefused, UsedRequests, resources_for

from .conftest import TEST_DATABASE_URL
from .test_identity_status import issue, run
from .test_mcp_auth import whoami
from .test_oauth import AUTH, ISS, MCP, _settings, live, signed, token_request
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import clean_chain, engine  # noqa: F401
from .test_signer import DID, KEY, _running, remote

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
TOKEN_KEY = generate_private_key(public_exponent=65537, key_size=2048)
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- no database


def test_the_signer_signs_tokens_for_its_own_mcp_server_only() -> None:
    assert resources_for("did:web:api.debitmydata.io") == (
        "https://api.debitmydata.io/agenticsuperhighway",
        "https://api.debitmydata.io/mcp",
    )
    assert resources_for("did:web:localhost%3A8000") == (MCP, "http://localhost:8000/mcp")


def test_the_signer_signs_tokens_only_with_its_key_and_the_authorization_server(
    tmp_path: Path,
) -> None:
    path = tmp_path / "oauth.pem"
    keys.write_new_key(path)
    did = "did:web:api.debitmydata.io"
    assert _token_issuer({}, did) is None
    assert _token_issuer({"DMD_SIGNER_OAUTH_KEY_PATH": str(path)}, did) is None
    issuer = _token_issuer(
        {"DMD_SIGNER_OAUTH_KEY_PATH": str(path), "DMD_SIGNER_OAUTH_ISSUER": ISS}, did
    )
    assert issuer is not None and issuer.issuer == ISS
    for bad in ("http://auth.debitmydata.io", "https://auth.debitmydata.io/token"):
        with pytest.raises(RuntimeError, match="https://<host> only"):
            _token_issuer(
                {"DMD_SIGNER_OAUTH_KEY_PATH": str(path), "DMD_SIGNER_OAUTH_ISSUER": bad}, did
            )


def test_each_signed_request_once() -> None:
    used = UsedRequests()
    expires = NOW + timedelta(minutes=1)
    used.use("did:a", "1", expires, NOW)
    used.use("did:b", "1", expires, NOW)  # another DID-LLM's jti
    with pytest.raises(TokenRefused, match="already used"):
        used.use("did:a", "1", expires, NOW)
    # Forgotten only once it can no longer be accepted anyway.
    used.use("did:a", "2", expires, NOW + timedelta(minutes=10))
    assert ("did:a", "1") not in used.seen


def test_public_key_file_for_the_api(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "oauth.pem"
    keys.write_new_key(path)
    assert keys.main(["--public", str(path)]) == 0
    pem = capsys.readouterr().out
    assert pem.startswith("-----BEGIN PUBLIC KEY-----") and "PRIVATE" not in pem
    public = tmp_path / "oauth-public.pem"
    public.write_text(pem)
    loaded = keys.load_public_key(public)
    assert keys.public_jwk(loaded) == keys.public_jwk(keys.load_private_key(path).public_key())
    with pytest.raises(keys.OAuthKeyError):
        keys.load_public_key(tmp_path / "missing.pem")


def test_settings_for_tokens_signed_by_the_signer(tmp_path: Path) -> None:
    public = tmp_path / "oauth-public.pem"
    socket = {"issuer_signer_socket": "/run/dmd-signer/signer.sock"}
    socket["issuer_public_key"] = KEY.public_key_multibase
    with pytest.raises(ValidationError, match="needs issuer_signer_socket"):
        _settings(auth_host="auth.example.test", oauth_public_key_path=public)
    with pytest.raises(ValidationError, match="not both"):
        _settings(oauth_key_path=public, oauth_public_key_path=public, **socket)
    with pytest.raises(ValidationError, match="needs oauth_public_key_path"):
        _settings(
            environment="production",
            public_base_url="https://api.debitmydata.io",
            auth_host="auth.debitmydata.io",
            **socket,
        )
    production = _settings(
        environment="production",
        public_base_url="https://api.debitmydata.io",
        auth_host="auth.debitmydata.io",
        oauth_public_key_path=public,
        **socket,
    )
    assert production.oauth_public_key_path == public


# --------------------------------------------------------------------------- with the database


@pytest.fixture
def token_signer(engine: Engine) -> Iterator[Path]:  # noqa: F811
    tokens = TokenIssuer(key=TOKEN_KEY, issuer=ISS, resources=resources_for(DID))
    path, server = _running(Signer(key=KEY, sessions=sessionmaker(engine), tokens=tokens))
    yield path
    server.shutdown()
    server.server_close()


def _gateway(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
    socket: Path,
    public_key: Any,
    tmp_path: Path,
) -> Any:
    public = tmp_path / "oauth-public.pem"
    public.write_text(keys.public_pem(public_key))
    overrides = {
        "auth_host": AUTH,
        "oauth_public_key_path": public,
        "issuer_signer_socket": socket,
        "issuer_public_key": KEY.public_key_multibase,
    }
    return _app_client(
        ready_registry, engine, issuer_key=remote(socket), settings_overrides=overrides
    )


def ask(socket: Path, assertion: str, **changes: Any) -> dict[str, Any]:
    request = {
        "op": "token",
        "assertion": assertion,
        "resource": MCP,
        "scope": "did:read",
        "ttl_seconds": 600,
        **changes,
    }
    return call_signer(socket, request, 5)


@needs_db
def test_sign_in_through_the_signer(
    token_signer: Path,
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    with _gateway(ready_registry, engine, token_signer, TOKEN_KEY.public_key(), tmp_path) as client:
        checks = client.get("/readyz").json()["checks"]
        assert checks["credential_signer"] == checks["token_signer"] == "ok"
        jwks = client.get("/.well-known/jwks.json", headers={"host": AUTH}).json()
        assert jwks == keys.jwk_set(TOKEN_KEY.public_key())

        did = issue(client)["did"]
        response = token_request(client, live(did))
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        claims = verify_token(
            token, TOKEN_KEY.public_key(), issuer=ISS, audiences=[MCP], now=datetime.now(UTC)
        )
        assert claims["sub"] == did
        answer = whoami(client, token).json()["result"]["structuredContent"]
        assert answer["signed_in_as"] == did

        # The API can refuse more (here: a replay it remembers), never less than the signer.
        assertion = live(did)
        assert token_request(client, assertion).status_code == 200
        replay = token_request(client, assertion)
        assert replay.status_code == 401 and "already used" in replay.text

        # A suspended DID-LLM: refused by the API, and by the signer even if the API asked.
        assertion = live(did)
        code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
        assert code == 0
        with pytest.raises(SignerRefused, match="suspended"):
            ask(token_signer, assertion)


@needs_db
def test_the_signer_checks_everything_itself(
    token_signer: Path,
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    """What a compromised API could ask: the signer refuses it all on its own."""
    with _gateway(ready_registry, engine, token_signer, TOKEN_KEY.public_key(), tmp_path) as client:
        did = issue(client)["did"]
    described = call_signer(token_signer, {"op": "describe"}, 5)
    assert described["token_key"] == keys.public_jwk(TOKEN_KEY.public_key())

    assertion = live(did)
    answer = ask(token_signer, assertion)
    assert answer["expires_in"] == 600 and answer["scope"] == "did:read"
    for request, reason in (
        ({"assertion": assertion}, "already used"),
        ({"resource": "https://bank.example/api"}, "another service"),
        ({"scope": "did:write"}, "unknown scope"),
        ({"scope": ""}, "unknown scope"),
        ({"ttl_seconds": 86400}, "60 to 3600"),
        ({"ttl_seconds": True}, "ttl_seconds"),
        ({"assertion": live(did, aud="https://auth.elsewhere.example/token")}, "aud must be"),
        ({"assertion": live(did, key=Ed25519PrivateKey.generate())}, "signature"),
        ({"assertion": live("did:web:unknown.example:agents:x")}, "unknown DID-LLM"),
        ({"assertion": signed(did, now=datetime.now(UTC) - timedelta(hours=1))}, "expired"),
    ):
        fields = {"assertion": live(did), **request}
        with pytest.raises(SignerRefused, match=reason):
            ask(token_signer, **fields)


@needs_db
def test_when_the_signer_cannot_sign_tokens_nobody_signs_in(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    # A signer without a token key: not ready, and signing in answers 503 (try again later).
    path, server = _running(Signer(key=KEY, sessions=sessionmaker(engine)))
    try:
        with _gateway(ready_registry, engine, path, TOKEN_KEY.public_key(), tmp_path) as client:
            did = issue(client)["did"]
            assert client.get("/readyz").json()["checks"]["token_signer"] == "unavailable"
            response = token_request(client, live(did))
            assert response.status_code == 503, response.text
            assert response.json()["error"] == "temporarily_unavailable"
            assert response.headers["retry-after"] == "60"
    finally:
        server.shutdown()
        server.server_close()

    # A signer whose token key is not the one the API expects: its tokens are never used.
    other = generate_private_key(public_exponent=65537, key_size=2048)
    tokens = TokenIssuer(key=other, issuer=ISS, resources=resources_for(DID))
    path, server = _running(Signer(key=KEY, sessions=sessionmaker(engine), tokens=tokens))
    try:
        with _gateway(ready_registry, engine, path, TOKEN_KEY.public_key(), tmp_path) as client:
            did = issue(client)["did"]
            assert client.get("/readyz").json()["checks"]["token_signer"] == "unavailable"
            response = token_request(client, live(did))
            assert response.status_code == 503, response.text
            assert "access_token" not in response.text
        # ... and with the signer gone entirely.
        server.shutdown()
        server.server_close()
        with pytest.raises(SignerUnavailable):
            call_signer(path, {"op": "describe"}, 1)
    finally:
        server.server_close()
