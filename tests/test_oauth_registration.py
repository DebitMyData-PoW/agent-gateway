"""Google's apps sign people in (oauth/registration.py): they register (RFC 7591) with Google's
own return addresses only, authenticate with a client secret, keep one refresh token, and
get tokens for the identity endpoint only.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from dmd_gateway.db.models import OAuthRegisteredClient
from dmd_gateway.db.session import session_factory
from dmd_gateway.identity.wallets import WalletVerifier
from dmd_gateway.oauth import keys, registration
from dmd_gateway.oauth.clients import ClientDirectory, ClientRejected
from dmd_gateway.oauth.tokens import verify_token

from .conftest import TEST_DATABASE_URL
from .fakes import sign_challenge
from .test_identity_endpoint import IDENTITY, call, result
from .test_identity_status import issue
from .test_oauth import AUTH, ISS, MCP
from .test_oauth_browser import answer_of, at_auth, fake_fetch, pkce
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import ISSUER, clean_chain, engine  # noqa: F401

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

GOOGLE = "https://oauth-redirect.googleusercontent.com/r/debitmydata-test"
GOOGLE_SANDBOX = "https://oauth-redirect-sandbox.googleusercontent.com/r/debitmydata-test"


# --------------------------------------------------------------------------- without a server


@pytest.mark.parametrize(
    "uri",
    [
        GOOGLE,
        GOOGLE_SANDBOX,
        "https://oauth-redirect-test.googleusercontent.com/a/project.app/flip",
    ],
)
def test_googles_return_addresses_are_accepted(uri: str) -> None:
    assert registration.GOOGLE_REDIRECT.match(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://app.example.com/callback",
        "http://oauth-redirect.googleusercontent.com/r/x",
        "https://oauth-redirect.googleusercontent.com.evil.test/r/x",
        "https://evil.test/oauth-redirect.googleusercontent.com/r/x",
        "https://oauth-redirect.googleusercontent.com/r/",
        "https://oauth-redirect.googleusercontent.com/x/project",
        "https://oauth-redirect.googleusercontent.com/r/x?next=https://evil.test",
        "https://oauth-redirect.googleusercontent.com/r/x#frag",
        "https://oauth-redirect.googleusercontent.com/r/../x",
        "https://user@oauth-redirect.googleusercontent.com/r/x",
    ],
)
def test_other_return_addresses_are_refused(uri: str) -> None:
    assert not registration.GOOGLE_REDIRECT.match(uri)
    with pytest.raises(registration.RegistrationRefused) as caught:
        registration._redirect_uris({"redirect_uris": [uri]})
    assert caught.value.error == "invalid_redirect_uri"


def test_basic_credentials_are_read_as_oauth_sends_them() -> None:
    client_id = "https://oauth-redirect.googleusercontent.com/debitmydata-client/abc"
    secret = "s3cr:t+/="
    pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
    header = "Basic " + base64.b64encode(pair.encode()).decode()
    assert registration.basic_credentials(header) == (client_id, secret)
    for bad in (
        None,
        "",
        "Bearer x",
        "Basic !!!",
        "Basic " + base64.b64encode(b"nocolon").decode(),
    ):
        assert registration.basic_credentials(bad) is None


def test_registered_ids_are_never_fetched() -> None:
    fetched: list[str] = []

    def fetch(url: str) -> Any:
        fetched.append(url)
        raise AssertionError("fetched")

    directory = ClientDirectory(
        fetch=fetch, registered=lambda _: None, registered_hosts=registration.GOOGLE_HOSTS
    )
    with pytest.raises(ClientRejected, match="not registered"):
        directory.get("https://oauth-redirect.googleusercontent.com/debitmydata-client/x")
    assert fetched == []


# --------------------------------------------------------------------------- the server


@pytest.fixture
def gateway(ready_registry: Path, engine: Engine, tmp_path: Path) -> Iterator[TestClient]:  # noqa: F811
    key_path = tmp_path / "oauth.pem"
    keys.write_new_key(key_path)
    overrides = {
        "auth_host": AUTH,
        "oauth_key_path": key_path,
        "oauth_registration_enabled": True,
    }
    with _app_client(
        ready_registry, engine, issuer_key=ISSUER, settings_overrides=overrides
    ) as client:
        state = client.app.state  # type: ignore[attr-defined]
        state.oauth_clients = ClientDirectory(
            fetch=fake_fetch,
            registered=registration.database_lookup(state.session_factory),
            registered_hosts=registration.GOOGLE_HOSTS,
        )
        state.wallet_verifier = WalletVerifier({})
        client.key_path = key_path  # type: ignore[attr-defined]
        yield client


def register(client: TestClient, **metadata: Any) -> Any:
    body = {"client_name": "Gemini test", "redirect_uris": [GOOGLE, GOOGLE_SANDBOX], **metadata}
    return at_auth(client, "POST", "/register", json=body)


def basic(client_id: str, secret: str) -> dict[str, str]:
    pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
    return {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}


def sign_in(client: TestClient, app: dict[str, Any], did: str, **changes: Any) -> tuple[str, str]:
    """A person signs in to the registered app: its code and PKCE verifier."""
    verifier, challenge = pkce()
    params = {
        "response_type": "code",
        "client_id": app["client_id"],
        "redirect_uri": GOOGLE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "g1",
        "scope": "did:read",
        **changes,
    }
    started = at_auth(client, "GET", "/authorize", params=params)
    assert started.status_code == 303, started.text
    page = started.headers["location"]
    assert page.startswith("/authorize/dmd_signin_"), page
    html = at_auth(client, "GET", page).text
    assert "Gemini test (oauth-redirect.googleusercontent.com)" in html
    chosen = at_auth(client, "POST", f"{page}/did", json={"did": did}).json()
    lines = chosen["message"].split("\n")
    assert lines[1] == "Sign in to: Gemini test (oauth-redirect.googleusercontent.com)"
    assert f"For: {IDENTITY}" in lines  # the identity endpoint, by default
    signed = at_auth(
        client, "POST", f"{page}/signature", json={"signature": sign_challenge(chosen["message"])}
    )
    assert signed.status_code == 200, signed.text
    finished = at_auth(client, "POST", f"{page}/finish").json()
    assert finished["redirect"].startswith(GOOGLE + "?")
    return answer_of(finished["redirect"])["code"], verifier


@needs_db
def test_registration_is_off_unless_switched_on(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "oauth.pem"
    keys.write_new_key(key_path)
    overrides = {"auth_host": AUTH, "oauth_key_path": key_path}
    with _app_client(
        ready_registry, engine, issuer_key=ISSUER, settings_overrides=overrides
    ) as client:
        metadata = at_auth(client, "GET", "/.well-known/oauth-authorization-server").json()
        assert "registration_endpoint" not in metadata
        assert "client_secret_basic" not in metadata["token_endpoint_auth_methods_supported"]
        assert register(client).status_code == 404


@needs_db
def test_google_registers_with_its_own_return_addresses(gateway: TestClient) -> None:
    metadata = at_auth(gateway, "GET", "/.well-known/oauth-authorization-server").json()
    assert metadata["registration_endpoint"] == f"{ISS}/register"
    for name in (
        "token_endpoint_auth_methods_supported",
        "revocation_endpoint_auth_methods_supported",
    ):
        assert {"client_secret_basic", "client_secret_post"} <= set(metadata[name])
    assert metadata["client_id_metadata_document_supported"] is True  # other apps: as before
    assert f"{ISS}/register" in gateway.get("/llms.txt").text

    answer = register(gateway)
    assert answer.status_code == 201, answer.text
    assert answer.headers["cache-control"] == "no-store"
    app = answer.json()
    assert app["client_id"].startswith(
        "https://oauth-redirect.googleusercontent.com/debitmydata-client/"
    )
    assert len(app["client_secret"]) >= 40 and app["client_secret_expires_at"] == 0
    assert app["redirect_uris"] == [GOOGLE, GOOGLE_SANDBOX]
    assert app["token_endpoint_auth_method"] == "client_secret_basic"
    assert app["grant_types"] == ["authorization_code", "refresh_token"]
    assert app["scope"] == "did:read"
    with session_factory(gateway.app.state.engine)() as session:  # type: ignore[attr-defined]
        row = session.get(OAuthRegisteredClient, app["client_id"])
        assert row is not None and row.secret_hash != app["client_secret"]  # its SHA-256 only
        assert row.secret_hash == registration.secret_hash(app["client_secret"])

    for metadata_change, error in (
        ({"redirect_uris": ["https://app.example.com/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": []}, "invalid_redirect_uri"),
        ({"token_endpoint_auth_method": "none"}, "invalid_client_metadata"),
        ({"grant_types": ["client_credentials"]}, "invalid_client_metadata"),
        ({"scope": "did:write"}, "invalid_client_metadata"),
        ({"client_name": 42}, "invalid_client_metadata"),
    ):
        refused = register(gateway, **metadata_change)
        assert (refused.status_code, refused.json()["error"]) == (400, error), metadata_change
    not_json = at_auth(
        gateway, "POST", "/register", content=b"x", headers={"content-type": "text/plain"}
    )
    assert not_json.status_code == 400


@needs_db
def test_a_google_sign_in_with_its_secret_and_one_refresh_token(gateway: TestClient) -> None:
    app = register(gateway).json()
    did = issue(gateway)["did"]
    code, verifier = sign_in(gateway, app, did)

    # The code exchange needs the app's secret.
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": GOOGLE,
        "code_verifier": verifier,
    }
    unsigned = at_auth(gateway, "POST", "/token", data={**fields, "client_id": app["client_id"]})
    assert (unsigned.status_code, unsigned.json()["error"]) == (401, "invalid_client")
    wrong = at_auth(gateway, "POST", "/token", data=fields, headers=basic(app["client_id"], "x"))
    assert wrong.status_code == 401
    exchanged = at_auth(
        gateway,
        "POST",
        "/token",
        data=fields,
        headers=basic(app["client_id"], app["client_secret"]),
    )
    assert exchanged.status_code == 200, exchanged.text
    tokens = exchanged.json()
    public = keys.load_private_key(gateway.key_path).public_key()  # type: ignore[attr-defined]
    claims = verify_token(
        tokens["access_token"], public, issuer=ISS, audiences=[IDENTITY], now=datetime.now(UTC)
    )
    assert claims["sub"] == did and claims["client_id"] == app["client_id"]
    assert (
        result(call(gateway, "debitmydata_whoami", tokens["access_token"]))["signed_in_as"] == did
    )

    # Refreshing gives the same refresh token back, again and again (client_secret_post too).
    refresh = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    for _ in range(2):
        again = at_auth(
            gateway,
            "POST",
            "/token",
            data={**refresh, "client_id": app["client_id"], "client_secret": app["client_secret"]},
        )
        assert again.status_code == 200, again.text
        assert again.json()["refresh_token"] == tokens["refresh_token"]
        assert again.json()["access_token"] != tokens["access_token"]
    with session_factory(gateway.app.state.engine)() as session:  # type: ignore[attr-defined]
        row = session.get(OAuthRegisteredClient, app["client_id"])
        assert row is not None and row.last_used_at is not None

    # Unlinking in Google ends the sign-in: the refresh token stops.
    unauthenticated = at_auth(
        gateway,
        "POST",
        "/revoke",
        data={"token": tokens["refresh_token"], "client_id": app["client_id"]},
    )
    assert unauthenticated.status_code == 401
    revoked = at_auth(
        gateway,
        "POST",
        "/revoke",
        data={"token": tokens["refresh_token"]},
        headers=basic(app["client_id"], app["client_secret"]),
    )
    assert revoked.status_code == 200
    after = at_auth(
        gateway,
        "POST",
        "/token",
        data=refresh,
        headers=basic(app["client_id"], app["client_secret"]),
    )
    assert after.json()["error"] == "invalid_grant"


@needs_db
def test_google_apps_get_the_identity_endpoint_only(gateway: TestClient) -> None:
    app = register(gateway).json()
    verifier, challenge = pkce()
    asked = at_auth(
        gateway,
        "GET",
        "/authorize",
        params={
            "response_type": "code",
            "client_id": app["client_id"],
            "redirect_uri": GOOGLE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": MCP,  # the Agentic Superhighway: not for Google's apps
        },
    )
    assert asked.status_code == 303
    assert answer_of(asked.headers["location"])["error"] == "invalid_target"
    # And only to its own registered return addresses.
    elsewhere = at_auth(
        gateway,
        "GET",
        "/authorize",
        params={
            "response_type": "code",
            "client_id": app["client_id"],
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/someone-else",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert elsewhere.status_code == 400
    # An app with a secret sends it; one without may not invent one.
    did = issue(gateway)["did"]
    code, verifier = sign_in(gateway, app, did)
    stranger = at_auth(
        gateway,
        "POST",
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": GOOGLE,
            "code_verifier": verifier,
            "client_id": "https://app.example.com/oauth/client.json",
            "client_secret": "guess",
        },
    )
    assert (stranger.status_code, stranger.json()["error"]) == (401, "invalid_client")


@needs_db
def test_registrations_are_limited_and_unused_ones_forgotten(engine: Engine) -> None:  # noqa: F811
    now = datetime.now(UTC)
    metadata = {"redirect_uris": [GOOGLE]}
    with session_factory(engine)() as session:
        session.query(OAuthRegisteredClient).delete()
        first = registration.register(session, metadata, now - timedelta(days=8))
        used = registration.register(session, metadata, now - timedelta(days=8))
        used.row.last_used_at = now - timedelta(days=1)
        session.flush()
        with pytest.raises(registration.RegistrationRefused) as caught:
            registration.register(session, metadata, now, max_per_hour=0)
        assert (caught.value.status, caught.value.error) == (503, "temporarily_unavailable")
        # That call forgot the registration never used: the used one stays.
        kept = set(session.scalars(select(OAuthRegisteredClient.client_id)))
        assert first.row.client_id not in kept and used.row.client_id in kept
        with pytest.raises(registration.RegistrationRefused):
            registration.register(session, metadata, now, max_clients=1)
        session.rollback()
