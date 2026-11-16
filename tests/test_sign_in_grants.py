"""People signing in to apps, in production: the signer service checks each sign-in itself and
gives a grant, then signs every access token for the app against it (oauth/grants.py,
signer/tokens.py, RemoteTokenSigner).

Covers: the sign-in message as the signer reads it, and every refusal; the owner's signature
checked with no network (key, ordinary wallet; a smart wallet is refused); grants (never access
tokens, 30 days, bound to the app, the MCP server and the scope); the whole browser sign-in
through a real signer on a Unix socket, with a key and with a wallet; what a compromised API
could ask the signer, refused; and that nothing is lost while the signer is down.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from eth_account import Account
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from dmd_gateway.identity.crypto import encode_signature
from dmd_gateway.identity.wallets import ERC1271, WalletVerifier
from dmd_gateway.oauth import grants
from dmd_gateway.oauth.clients import ClientDirectory
from dmd_gateway.oauth.grants import GrantRefused
from dmd_gateway.oauth.tokens import TokenRejected, issue_token, verify_token
from dmd_gateway.signer.client import RemoteTokenSigner, SignerRefused, call_signer
from dmd_gateway.signer.service import Signer
from dmd_gateway.signer.tokens import TokenIssuer, resources_for

from .conftest import TEST_DATABASE_URL
from .fakes import AGENT_CONTROLLER, sign_challenge
from .test_identity_status import issue, run
from .test_mcp_auth import whoami
from .test_oauth import ISS, MCP
from .test_oauth_browser import (
    APP,
    CALLBACK,
    answer_of,
    at_auth,
    code_for,
    fake_fetch,
    gateway,  # noqa: F401
    pkce,
    start,
    token,
)
from .test_quotes import ready_registry  # noqa: F401
from .test_registration import (  # noqa: F401
    answer,
    challenge,
    clean_chain,
    engine,
    quote,
    register,
    settle,
)
from .test_signer import DID, KEY, _running
from .test_signer_tokens import _gateway
from .test_wallet_registration import wallet_signature

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
TOKEN_KEY = generate_private_key(public_exponent=65537, key_size=2048)
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
OTHER_MCP = "http://localhost:8000/mcp"
OWNER = "did:web:localhost%3A8000:agents:01abc"


def message(**changes: Any) -> str:
    fields: dict[str, Any] = {
        "app": "Example App (app.example.com)",
        "did": OWNER,
        "scope": "did:read",
        "resource": MCP,
        "issuer": ISS,
        "nonce": "n0nce",
        "issued": NOW,
        "expires": NOW + timedelta(minutes=15),
        **changes,
    }
    return grants.sign_in_message(**fields)


def check(text: str, **changes: Any) -> grants.SignInMessage:
    fields: dict[str, Any] = {
        "did": OWNER,
        "client_id": APP,
        "scope": "did:read",
        "resource": MCP,
        "issuer": ISS,
        "resources": (MCP, OTHER_MCP),
        "now": NOW + timedelta(minutes=2),
        **changes,
    }
    return grants.check_sign_in(text, **fields)


# --------------------------------------------------------------------------- no database


def test_the_message_reads_back_exactly() -> None:
    read = grants.parse_sign_in_message(message(app="My (beta) App (app.example.com)"))
    assert read.app == "My (beta) App (app.example.com)" and read.app_host == "app.example.com"
    assert (read.did, read.resource, read.issuer, read.nonce) == (OWNER, MCP, ISS, "n0nce")
    assert read.allows == "read your own DID-LLM's records"
    assert read.expires - read.issued == timedelta(minutes=15)
    assert check(message()).nonce == "n0nce"


@pytest.mark.parametrize(
    ("text", "changes", "reason"),
    [
        (message(issuer="https://auth.elsewhere.example"), {}, "another sign-in server"),
        (message(resource="https://bank.example/mcp"), {}, "another service"),
        (message(), {"resource": "https://bank.example/mcp"}, "another service"),
        (message(resource=OTHER_MCP), {}, "another service"),
        (message(), {"did": "did:web:localhost%3A8000:agents:other"}, "another DID-LLM"),
        (message(), {"client_id": "https://evil.example/client.json"}, "another app"),
        (message(scope="did:write"), {}, "allows something else"),
        (message(), {"scope": "did:read did:write"}, "allows something else"),
        (message(), {"now": NOW + timedelta(minutes=20)}, "expired"),
        (message(), {"now": NOW - timedelta(minutes=5)}, "future"),
        (message(expires=NOW + timedelta(hours=1)), {}, "15 minutes"),
        (message(nonce="x" * 65), {}, "nonce"),
        (message(app="No site"), {}, "the app's site"),
        (message().replace("Nonce:", "Nonse:"), {}, "'Nonce' is missing"),
        (message() + "\nAlso: transfer everything", {}, "not a DebitMyData sign-in message"),
        ("Sign anything", {}, "not a DebitMyData sign-in message"),
        (message().replace("2026-11-14T12:00:00Z", "noon"), {}, "not dates"),
    ],
)
def test_messages_the_signer_refuses(text: str, changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(GrantRefused, match=reason):
        check(text, **changes)


def test_owner_signatures_are_checked_with_no_network() -> None:
    text = message()
    assert grants.owner_signed("ed25519", AGENT_CONTROLLER, text, sign_challenge(text))
    assert not grants.owner_signed("ed25519", AGENT_CONTROLLER, text, sign_challenge("other"))
    other = encode_signature(Ed25519PrivateKey.generate().sign(text.encode()))
    assert not grants.owner_signed("ed25519", AGENT_CONTROLLER, text, other)

    owner, stranger = Account.create(), Account.create()
    wallet = f"eip155:8453:{owner.address}"
    assert grants.owner_signed("eip155", wallet, text, wallet_signature(owner, text))
    assert not grants.owner_signed("eip155", wallet, text, wallet_signature(stranger, text))
    # A smart wallet's signature (longer, or a contract's): it would need the chain.
    assert not grants.owner_signed("eip155", wallet, text, "0x" + "ab" * 120)
    with pytest.raises(GrantRefused, match="cannot be read"):
        grants.owner_signed("eip155", wallet, text, "not hex")
    with pytest.raises(GrantRefused, match="unknown kind"):
        grants.owner_signed("bitcoin", wallet, text, "00")


def test_grants_are_never_access_tokens() -> None:
    grant = grants.issue_grant(
        TOKEN_KEY,
        issuer=ISS,
        did=OWNER,
        client_id=APP,
        scope="did:read",
        resource=MCP,
        nonce="n0nce",
        now=NOW,
    )
    public = (TOKEN_KEY.public_key(),)
    assert grant.expires_at == NOW + timedelta(days=30)
    read = grants.verify_grant(grant.token, public, issuer=ISS, now=NOW)
    assert (read.did, read.client_id, read.scope, read.resource) == (OWNER, APP, "did:read", MCP)
    with pytest.raises(TokenRejected, match="not an access token"):
        verify_token(grant.token, public, issuer=ISS, audiences=[MCP], now=NOW)
    access = issue_token(
        TOKEN_KEY,
        issuer=ISS,
        did=OWNER,
        audience=MCP,
        scopes=("did:read",),
        ttl_seconds=600,
        now=NOW,
    )
    with pytest.raises(GrantRefused, match="not a sign-in grant"):
        grants.verify_grant(access.access_token, public, issuer=ISS, now=NOW)
    with pytest.raises(GrantRefused, match="30 days"):
        grants.verify_grant(grant.token, public, issuer=ISS, now=NOW + timedelta(days=31))
    other = (generate_private_key(public_exponent=65537, key_size=2048).public_key(),)
    with pytest.raises(GrantRefused, match="unknown key"):
        grants.verify_grant(grant.token, other, issuer=ISS, now=NOW)
    with pytest.raises(GrantRefused, match="not valid"):
        grants.verify_grant(grant.token, public, issuer="https://auth.elsewhere.example", now=NOW)

    grants.check_use(read, client_id=APP, resource=MCP, scopes=("did:read",))
    for changes, reason in (
        ({"client_id": "https://evil.example/client.json"}, "another app"),
        ({"resource": OTHER_MCP}, "another service"),
        ({"scopes": ("did:read", "did:write")}, "more than the person did"),
        ({"scopes": ()}, "more than the person did"),
    ):
        use: dict[str, Any] = {
            "client_id": APP,
            "resource": MCP,
            "scopes": ("did:read",),
            **changes,
        }
        with pytest.raises(GrantRefused, match=reason):
            grants.check_use(read, **use)


# --------------------------------------------------------------------------- with the database


@pytest.fixture
def production(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Path]]:
    """The gateway as in production: tokens and grants signed by the signer service."""
    tokens = TokenIssuer(key=TOKEN_KEY, issuer=ISS, resources=resources_for(DID))
    path, server = _running(Signer(key=KEY, sessions=sessionmaker(engine), tokens=tokens))
    try:
        with _gateway(ready_registry, engine, path, TOKEN_KEY.public_key(), tmp_path) as client:
            client.app.state.oauth_clients = ClientDirectory(fetch=fake_fetch)  # type: ignore[attr-defined]
            client.app.state.wallet_verifier = WalletVerifier({})  # type: ignore[attr-defined]
            yield client, path
    finally:
        server.shutdown()
        server.server_close()


def exchange(client: TestClient, did: str) -> dict[str, Any]:
    verifier, challenge_value = pkce()
    code = code_for(client, did, challenge_value)
    response = token(
        client,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
    )
    assert response.status_code == 200, response.text
    return response.json()


def ask_grant(socket: Path, **fields: Any) -> dict[str, Any]:
    return call_signer(socket, {"op": "sign_in", **fields}, 5)


def ask_token(socket: Path, grant: str, **changes: Any) -> dict[str, Any]:
    request = {
        "op": "token",
        "grant": grant,
        "client_id": APP,
        "resource": MCP,
        "scope": "did:read",
        "ttl_seconds": 600,
        **changes,
    }
    return call_signer(socket, request, 5)


def stored_grants(engine: Engine) -> list[tuple[str, datetime]]:  # noqa: F811
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT sign_in_grant, expires_at FROM oauth_refresh_tokens ORDER BY created_at")
        )
        return [(row[0], row[1]) for row in rows]


@needs_db
def test_people_sign_in_to_apps_through_the_signer(
    production: tuple[TestClient, Path],
    engine: Engine,  # noqa: F811
) -> None:
    client, _ = production
    meta = at_auth(client, "GET", "/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"] == f"{ISS}/authorize"
    assert "authorization_code" in meta["grant_types_supported"]
    assert "authorization code flow" in client.get("/llms.txt").text

    did = issue(client)["did"]
    tokens = exchange(client, did)
    claims = verify_token(
        tokens["access_token"],
        TOKEN_KEY.public_key(),
        issuer=ISS,
        audiences=[MCP],
        now=datetime.now(UTC),
    )
    assert claims["sub"] == did and claims["client_id"] == APP
    assert claims["sid"].startswith("dmd_oauthfam_")  # the sign-in, written by the signer
    me = whoami(client, tokens["access_token"]).json()["result"]["structuredContent"]
    assert me["signed_in_as"] == did

    # The refresh token carries the grant, and never outlives it (30 days from the signature).
    [(grant, until)] = stored_grants(engine)
    read = grants.verify_grant(grant, (TOKEN_KEY.public_key(),), issuer=ISS, now=datetime.now(UTC))
    assert read.did == did and read.client_id == APP and until <= read.expires_at
    refreshed = token(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text
    assert stored_grants(engine)[-1][0] == grant

    # Suspended: the API refuses the refresh, and the signer would refuse the token anyway.
    code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code == 0
    newest = refreshed.json()["refresh_token"]
    refused = token(client, grant_type="refresh_token", refresh_token=newest)
    assert refused.json()["error"] == "invalid_grant" and "suspended" in refused.text


@needs_db
def test_the_signer_checks_each_sign_in_itself(
    production: tuple[TestClient, Path],
    engine: Engine,  # noqa: F811
) -> None:
    """What a compromised API could ask: the signer refuses it all on its own."""
    client, socket = production
    did = issue(client)["did"]
    _, challenge_value = pkce()
    page = start(client, challenge_value)
    chosen = at_auth(client, "POST", f"{page}/did", json={"did": did}).json()
    text_ = chosen["message"]
    signature = sign_challenge(text_)
    request = {
        "message": text_,
        "signature": signature,
        "did": did,
        "controller_type": "ed25519",
        "client_id": APP,
        "scope": "did:read",
        "resource": MCP,
    }
    forged = encode_signature(Ed25519PrivateKey.generate().sign(text_.encode()))
    for changes, reason in (
        ({"signature": forged}, "not the DID-LLM owner's"),
        ({"signature": sign_challenge(text_ + " ")}, "not the DID-LLM owner's"),
        ({"controller_type": "eip155"}, "no such owner key or wallet"),
        ({"client_id": "https://evil.example/client.json"}, "another app"),
        ({"resource": "http://localhost:8000/mcp"}, "another service"),
        ({"did": "did:web:localhost%3A8000:agents:someone"}, "another DID-LLM"),
        ({"message": text_.replace(did, "did:web:x"), "did": "did:web:x"}, "unknown DID-LLM"),
        ({"scope": ""}, "a sign-in needs"),
    ):
        with pytest.raises(SignerRefused, match=reason):
            ask_grant(socket, **{**request, **changes})
    grant = ask_grant(socket, **request)["grant"]
    # The same signed message twice: once only.
    with pytest.raises(SignerRefused, match="already used"):
        ask_grant(socket, **request)

    # Tokens under the grant: for that app, that MCP server and that scope only.
    assert ask_token(socket, grant)["scope"] == "did:read"
    for changes, reason in (
        ({"client_id": "https://evil.example/client.json"}, "another app"),
        ({"resource": "http://localhost:8000/mcp"}, "another service"),
        ({"resource": "https://bank.example/api"}, "another service"),
        ({"scope": "did:write"}, "unknown scope"),
        ({"ttl_seconds": 86400}, "60 to 3600"),
        ({"grant": grant[:-4] + "AAAA"}, "not valid"),
    ):
        with pytest.raises(SignerRefused, match=reason):
            ask_token(socket, **{"grant": grant, **changes})
    other_key = generate_private_key(public_exponent=65537, key_size=2048)
    for key, days_ago, reason in ((other_key, 0, "unknown key"), (TOKEN_KEY, 31, "30 days")):
        made = grants.issue_grant(
            key,
            issuer=ISS,
            did=did,
            client_id=APP,
            scope="did:read",
            resource=MCP,
            nonce="n",
            now=datetime.now(UTC) - timedelta(days=days_ago),
        )
        with pytest.raises(SignerRefused, match=reason):
            ask_token(socket, made.token)
    code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code == 0
    with pytest.raises(SignerRefused, match="suspended"):
        ask_token(socket, grant)


def _wallet_did(client: TestClient) -> tuple[str, Any]:
    owner = Account.create()
    q = quote(client, controller_public_key=None, controller_wallet_address=owner.address)
    rid = q["registration_intent_id"]
    c = challenge(client, rid)
    proved = answer(client, rid, c["challenge_id"], wallet_signature(owner, c["message"]))
    assert proved.status_code == 201, proved.text
    settle(client, q, txid=secrets.token_hex(32))
    return register(client, rid).json()["did"], owner


class SmartWallets:
    """Stands in for a wallet verifier that asked the chain: a smart wallet, rightly signed."""

    def verify(self, wallet: Any, message: str, signature: bytes) -> str:
        return ERC1271


@needs_db
def test_wallets_sign_in_through_the_signer_smart_wallets_not_yet(
    production: tuple[TestClient, Path],
) -> None:
    client, socket = production
    did, owner = _wallet_did(client)
    verifier, challenge_value = pkce()
    page = start(client, challenge_value)
    chosen = at_auth(client, "POST", f"{page}/did", json={"did": did}).json()
    assert chosen["controller"]["type"] == "wallet"
    signed = at_auth(
        client,
        "POST",
        f"{page}/signature",
        json={"signature": wallet_signature(owner, chosen["message"])},
    )
    assert signed.status_code == 200, signed.text
    code = answer_of(at_auth(client, "POST", f"{page}/finish").json()["redirect"])["code"]
    exchanged = token(
        client,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
    )
    assert exchanged.status_code == 200, exchanged.text

    # A smart wallet, even rightly signed (the API asked its chain): not in production yet,
    # and not a wrong try.
    client.app.state.wallet_verifier = SmartWallets()  # type: ignore[attr-defined]
    page = start(client, challenge_value)
    chosen = at_auth(client, "POST", f"{page}/did", json={"did": did}).json()
    smart = "0x" + "ab" * 120
    refused = at_auth(client, "POST", f"{page}/signature", json={"signature": smart})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "smart_wallet_not_supported"
    assert "key" in refused.json()["error"]["message"]
    assert at_auth(client, "GET", f"{page}/request").json()["status"] == "open"
    # ... and the signer would refuse it anyway, whatever the API says.
    with pytest.raises(SignerRefused, match="smart wallet"):
        ask_grant(
            socket,
            message=chosen["message"],
            signature=smart,
            did=did,
            controller_type="eip155",
            client_id=APP,
            scope="did:read",
            resource=MCP,
        )


@needs_db
def test_staging_accepts_smart_wallets_its_api_checked(gateway: TestClient) -> None:  # noqa: F811
    did, _ = _wallet_did(gateway)
    gateway.app.state.wallet_verifier = SmartWallets()  # type: ignore[attr-defined]
    _, challenge_value = pkce()
    page = start(gateway, challenge_value)
    chosen = at_auth(gateway, "POST", f"{page}/did", json={"did": did}).json()
    signed = at_auth(gateway, "POST", f"{page}/signature", json={"signature": "0x" + "ab" * 120})
    assert signed.status_code == 200, signed.text
    assert chosen["message"].startswith("DebitMyData sign-in\n")


@needs_db
def test_while_the_signer_is_down_nothing_is_lost(
    production: tuple[TestClient, Path],
    tmp_path: Path,
) -> None:
    client, _ = production
    state = client.app.state  # type: ignore[attr-defined]
    signer = state.token_signer
    down = RemoteTokenSigner(socket_path=tmp_path / "gone.sock", public_key=TOKEN_KEY.public_key())
    did = issue(client)["did"]
    verifier, challenge_value = pkce()
    page = start(client, challenge_value)
    chosen = at_auth(client, "POST", f"{page}/did", json={"did": did}).json()
    signature = {"signature": sign_challenge(chosen["message"])}

    state.token_signer = down
    waiting = at_auth(client, "POST", f"{page}/signature", json=signature)
    assert waiting.status_code == 503 and waiting.headers["retry-after"] == "30"
    assert waiting.json()["error"]["code"] == "sign_in_unavailable"
    state.token_signer = signer
    still = at_auth(client, "GET", f"{page}/request").json()
    assert still["status"] == "open"
    assert at_auth(client, "POST", f"{page}/signature", json=signature).status_code == 200
    code = answer_of(at_auth(client, "POST", f"{page}/finish").json()["redirect"])["code"]

    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "code_verifier": verifier,
    }
    state.token_signer = down
    later = token(client, **fields)
    assert later.status_code == 503 and later.headers["retry-after"] == "60"
    assert later.json()["error"] == "temporarily_unavailable"
    state.token_signer = signer
    exchanged = token(client, **fields)  # the code is still good, within its minute
    assert exchanged.status_code == 200, exchanged.text

    state.token_signer = down
    refresh = {"grant_type": "refresh_token", "refresh_token": exchanged.json()["refresh_token"]}
    assert token(client, **refresh).status_code == 503
    state.token_signer = signer
    assert token(client, **refresh).status_code == 200


@needs_db
def test_sign_ins_from_before_grants_sign_in_again(
    gateway: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    did = issue(gateway)["did"]
    tokens = exchange(gateway, did)
    with engine.begin() as connection:
        connection.execute(text("UPDATE oauth_refresh_tokens SET sign_in_grant = NULL"))
    again = token(gateway, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert again.json()["error"] == "invalid_grant" and "sign in again" in again.text
