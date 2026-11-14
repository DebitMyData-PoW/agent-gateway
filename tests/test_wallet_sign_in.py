"""Agents owned by a wallet sign in with it (the runbook's wallet-signature path,
oauth/wallet_sign_in.py): the message from /challenge, signed with the wallet, for an access
token. Covers the message and every refusal; the whole sign-in on staging (an ordinary and a
smart wallet) and in production through the signer (which checks the message and signature
itself, an ordinary wallet only); single use; and that nothing is lost while the signer is down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from dmd_gateway.identity.wallets import WalletVerifier
from dmd_gateway.oauth import wallet_sign_in as wallet
from dmd_gateway.oauth.grants import GrantRefused
from dmd_gateway.oauth.tokens import verify_token
from dmd_gateway.signer.client import RemoteTokenSigner, SignerRefused, call_signer

from .conftest import TEST_DATABASE_URL
from .test_identity_status import issue, run
from .test_mcp_auth import whoami
from .test_oauth import ISS, MCP
from .test_oauth_browser import at_auth, gateway  # noqa: F401
from .test_quotes import ready_registry  # noqa: F401
from .test_registration import clean_chain, engine  # noqa: F401
from .test_sign_in_grants import TOKEN_KEY, SmartWallets, _wallet_did, production  # noqa: F401
from .test_wallet_registration import wallet_signature

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
OWNER = "did:web:localhost%3A8000:agents:01wallet"
OTHER_MCP = "http://localhost:8000/mcp"


def message(**changes: Any) -> str:
    fields: dict[str, Any] = {
        "did": OWNER,
        "scope": "did:read",
        "resource": MCP,
        "issuer": ISS,
        "now": NOW,
        **changes,
    }
    return wallet.challenge(**fields)


def check(text: str, **changes: Any) -> tuple[wallet.WalletMessage, str]:
    fields: dict[str, Any] = {
        "issuer": ISS,
        "resources": (MCP, OTHER_MCP),
        "now": NOW + timedelta(minutes=1),
        **changes,
    }
    return wallet.check(text, **fields)


# --------------------------------------------------------------------------- no database


def test_the_message() -> None:
    text = message()
    lines = text.split("\n")
    assert lines[:5] == [
        "DebitMyData agent sign-in",
        f"DID-LLM: {OWNER}",
        "Allows: read your own DID-LLM's records",
        f"For: {MCP}",
        f"Sign-in server: {ISS}",
    ]
    assert lines[6:] == ["Issued: 2026-11-14T12:00:00Z", "Expires: 2026-11-14T12:05:00Z"]
    read, scope = check(text)
    assert (read.did, read.resource, scope) == (OWNER, MCP, "did:read")
    assert message() != message()  # a new nonce each time
    assert check(text, scope="did:read", resource=MCP)[1] == "did:read"
    assert check(text.replace("\n", "\r\n"))[0].did == OWNER  # as some tools send it


@pytest.mark.parametrize(
    ("text", "changes", "reason"),
    [
        (message(issuer="https://auth.elsewhere.example"), {}, "another sign-in server"),
        (message(resource="https://bank.example/mcp"), {}, "another service"),
        (message(), {"resource": OTHER_MCP}, "another service"),
        (message(scope="did:write"), {}, "something unknown"),
        (message(), {"scope": "did:write"}, "scope must be the one in the message"),
        (message(), {"now": NOW + timedelta(minutes=7)}, "expired"),
        (message(), {"now": NOW - timedelta(minutes=5)}, "future"),
        (message().replace("12:05:00Z", "13:00:00Z"), {}, "5 minutes at most"),
        (message() + "\nAlso: transfer everything", {}, "ask /challenge"),
        (message().replace("DebitMyData agent", "DebitMyData"), {}, "ask /challenge"),
        (message().replace("Nonce:", "Nonse:"), {}, "'Nonce' is missing"),
    ],
)
def test_messages_that_are_refused(text: str, changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(GrantRefused, match=reason):
        check(text, **changes)


# --------------------------------------------------------------------------- with the database


def ask(client: TestClient, did: str, **fields: str) -> Any:
    return at_auth(client, "POST", "/challenge", data={"did": did, **fields})


def sign_in(client: TestClient, text: str, signature: str, **fields: str) -> Any:
    form = {"grant_type": wallet.GRANT_TYPE, "message": text, "signature": signature, **fields}
    return at_auth(client, "POST", "/token", data=form)


def token_for(client: TestClient, did: str, owner: Any) -> dict[str, Any]:
    answer = ask(client, did)
    assert answer.status_code == 200, answer.text
    text = answer.json()["message"]
    signed = sign_in(client, text, wallet_signature(owner, text))
    assert signed.status_code == 200, signed.text
    return {**signed.json(), "message": text}


@needs_db
def test_an_agent_signs_in_with_its_wallet(gateway: TestClient, engine: Engine) -> None:  # noqa: F811
    did, owner = _wallet_did(gateway)
    answer = ask(gateway, did).json()
    assert answer["wallet"].endswith(owner.address)
    assert answer["expires_in"] == 300 and answer["grant_type"] == wallet.GRANT_TYPE
    assert answer["token_endpoint"] == f"{ISS}/token"

    tokens = token_for(gateway, did, owner)
    claims = verify_token(
        tokens["access_token"],
        gateway.app.state.token_signer.public_keys,  # type: ignore[attr-defined]
        issuer=ISS,
        audiences=[MCP],
        now=datetime.now(UTC),
    )
    assert claims["sub"] == did and claims["client_id"] == did and claims["scope"] == "did:read"
    assert "refresh_token" not in tokens
    me = whoami(gateway, tokens["access_token"]).json()["result"]["structuredContent"]
    assert me["signed_in_as"] == did

    # Each message once; only the owner wallet; only well-formed signatures.
    again = sign_in(gateway, tokens["message"], wallet_signature(owner, tokens["message"]))
    assert again.json() == {
        "error": "invalid_grant",
        "error_description": "this message was already used: ask /challenge again",
    }
    text = ask(gateway, did).json()["message"]
    stranger = sign_in(gateway, text, wallet_signature(Account.create(), text))
    assert stranger.json()["error_description"] == "the signature is not the owner wallet's"
    garbled = sign_in(gateway, text, "not hex")
    assert garbled.json()["error"] == "invalid_request"
    assert (
        sign_in(gateway, text, wallet_signature(owner, text), client_id="did:web:x").json()["error"]
        == "invalid_client"
    )
    old = wallet.challenge(
        did=did,
        scope="did:read",
        resource=MCP,
        issuer=ISS,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    expired = sign_in(gateway, old, wallet_signature(owner, old))
    assert "expired" in expired.json()["error_description"]
    assert sign_in(gateway, text, "").json()["error"] == "invalid_request"

    # Who cannot: an unknown DID-LLM, one owned by a key only, a suspended one.
    assert (
        ask(gateway, "did:web:localhost%3A8000:agents:nobody").json()["error_description"]
        == "unknown DID-LLM"
    )
    keyed = issue(gateway)["did"]
    assert "sign in with its key" in ask(gateway, keyed).json()["error_description"]
    assert ask(gateway, did, resource="https://bank.example/mcp").json()["error"] == (
        "invalid_target"
    )
    assert ask(gateway, did, scope="did:write").json()["error"] == "invalid_scope"
    assert at_auth(gateway, "POST", "/challenge", data={}).json()["error"] == "invalid_request"
    text = ask(gateway, did).json()["message"]
    code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code == 0
    assert ask(gateway, did).json()["error_description"] == "this DID-LLM is suspended"
    suspended = sign_in(gateway, text, wallet_signature(owner, text))
    assert suspended.json()["error_description"] == "this DID-LLM is suspended"

    # Only at the sign-in server's own address.
    assert gateway.post("/challenge", data={"did": did}).status_code == 404


@needs_db
def test_staging_accepts_a_smart_wallet_its_chain_confirmed(gateway: TestClient) -> None:  # noqa: F811
    did, _ = _wallet_did(gateway)
    gateway.app.state.wallet_verifier = SmartWallets()  # type: ignore[attr-defined]
    text = ask(gateway, did).json()["message"]
    assert sign_in(gateway, text, "0x" + "ab" * 120).status_code == 200


@needs_db
def test_in_production_the_signer_checks_the_wallet_itself(
    production: tuple[TestClient, Path],  # noqa: F811
    tmp_path: Path,
) -> None:
    client, socket = production
    did, owner = _wallet_did(client)
    tokens = token_for(client, did, owner)
    claims = verify_token(
        tokens["access_token"],
        TOKEN_KEY.public_key(),
        issuer=ISS,
        audiences=[MCP],
        now=datetime.now(UTC),
    )
    assert claims["sub"] == did

    def ask_signer(text: str, signature: str, **changes: Any) -> dict[str, Any]:
        request = {
            "op": "token",
            "wallet_message": text,
            "wallet_signature": signature,
            "resource": MCP,
            "scope": "did:read",
            "ttl_seconds": 600,
            **changes,
        }
        return call_signer(socket, request, 5)

    # What a compromised API could ask: the signer refuses it on its own.
    with pytest.raises(SignerRefused, match="already used"):
        ask_signer(tokens["message"], wallet_signature(owner, tokens["message"]))
    text = ask(client, did).json()["message"]
    with pytest.raises(SignerRefused, match="not the owner wallet's"):
        ask_signer(text, wallet_signature(Account.create(), text))
    with pytest.raises(SignerRefused, match="smart wallet"):
        ask_signer(text, "0x" + "ab" * 120)
    with pytest.raises(SignerRefused, match="another service"):
        ask_signer(text, wallet_signature(owner, text), resource="http://localhost:8000/mcp")
    keyed = issue(client)["did"]
    forged = wallet.challenge(
        did=keyed, scope="did:read", resource=MCP, issuer=ISS, now=datetime.now(UTC)
    )
    with pytest.raises(SignerRefused, match="no such owner key or wallet"):
        ask_signer(forged, wallet_signature(owner, forged))
    assert ask_signer(text, wallet_signature(owner, text))["scope"] == "did:read"

    # A smart wallet, even confirmed by its chain: not in production yet.
    client.app.state.wallet_verifier = SmartWallets()  # type: ignore[attr-defined]
    text = ask(client, did).json()["message"]
    smart = sign_in(client, text, "0x" + "ab" * 120)
    assert smart.status_code == 400 and "smart wallet" in smart.json()["error_description"]

    # The signer down: 503, and the same signed message works once it is back.
    client.app.state.wallet_verifier = WalletVerifier({})  # type: ignore[attr-defined]
    state = client.app.state  # type: ignore[attr-defined]
    signer = state.token_signer
    state.token_signer = RemoteTokenSigner(
        socket_path=tmp_path / "gone.sock", public_key=TOKEN_KEY.public_key()
    )
    text = ask(client, did).json()["message"]
    signature = wallet_signature(owner, text)
    down = sign_in(client, text, signature)
    assert down.status_code == 503 and down.headers["retry-after"] == "60"
    state.token_signer = signer
    assert sign_in(client, text, signature).status_code == 200
