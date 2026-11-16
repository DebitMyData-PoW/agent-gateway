"""Verifying an agent by its DID-LLM (POST /v1/dids/verifications, MCP debitmydata_verify_agent,
services/verification.py): status and signed credential, the challenge and the proof of
control by the owner's key or wallet, and everything that makes a DID-LLM not verified."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from dmd_gateway.identity.crypto import encode_signature
from dmd_gateway.oauth import wallet_sign_in
from dmd_gateway.oauth.grants import GrantRefused
from dmd_gateway.services import verification
from dmd_gateway.services.verification import challenge, clean_audience, read_proof

from .conftest import TEST_DATABASE_URL
from .fakes import sign_challenge
from .test_identity_status import issue, run
from .test_mcp_auth import rpc
from .test_oauth import ISS, MCP
from .test_oauth_browser import gateway  # noqa: F401
from .test_quotes import ready_registry  # noqa: F401
from .test_registration import clean_chain, engine  # noqa: F401
from .test_sign_in_grants import SmartWallets, _wallet_did
from .test_wallet_registration import wallet_signature

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
DID = "did:web:localhost%3A8000:agents:01KAAAAAAAAAAAAAAAAAAAAAAA"
HOST = {"host": "localhost:8000"}


# --------------------------------------------------------------------------- no database


def test_the_challenge() -> None:
    message, proof = challenge(DID, "Example  Service\n(evil: line)", NOW)
    assert message.split("\n") == [
        "DebitMyData proof of control",
        f"DID-LLM: {DID}",
        "To: Example Service (evil: line)",
        f"Nonce: {proof.nonce}",
        "Issued: 2026-11-14T12:00:00Z",
        "Expires: 2026-11-14T12:10:00Z",
    ]
    assert read_proof(message, DID, NOW + timedelta(minutes=9)) == proof
    assert clean_audience(None) == clean_audience("  ") == "a party verifying this DID-LLM"
    assert clean_audience("x" * 500) == "x" * 100
    assert clean_audience("a\x00b c") == "a b c"


def variant(change: str) -> str:
    message, _ = challenge(DID, "Example", NOW)
    edits = {
        "long": ("12:10:00Z", "13:00:00Z"),
        "sign-in": ("DebitMyData proof of control", "DebitMyData agent sign-in"),
        "nonce": ("Nonce:", "Nonse:"),
    }
    if change == "extra":
        return message + "\nTo transfer: everything"
    old, new = edits.get(change, ("", ""))
    return message.replace(old, new) if old else message


@pytest.mark.parametrize(
    ("change", "did", "now", "reason"),
    [
        ("", "did:web:localhost%3A8000:agents:other", NOW, "another DID-LLM"),
        ("", DID, NOW + timedelta(minutes=12), "expired"),
        ("", DID, NOW - timedelta(minutes=5), "future"),
        ("long", DID, NOW, "10 minutes at most"),
        ("extra", DID, NOW, "not a DebitMyData proof of control"),
        ("sign-in", DID, NOW, "not a DebitMyData proof of control"),
        ("nonce", DID, NOW, "'Nonce' is missing"),
    ],
)
def test_proofs_that_are_refused(change: str, did: str, now: datetime, reason: str) -> None:
    with pytest.raises(GrantRefused, match=reason):
        read_proof(variant(change), did, now)


def test_a_sign_in_message_is_never_a_proof() -> None:
    """A signature made to sign in to DebitMyData is not accepted as a proof for anyone."""
    sign_in = wallet_sign_in.challenge(did=DID, scope="did:read", resource=MCP, issuer=ISS, now=NOW)
    with pytest.raises(GrantRefused, match="not a DebitMyData proof of control"):
        read_proof(sign_in, DID, NOW)
    assert verification.TITLE == "DebitMyData proof of control"


# --------------------------------------------------------------------------- with the database


def verify(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/v1/dids/verifications", json=body, headers=HOST)
    assert response.status_code == 200, response.text
    return response.json()


@needs_db
def test_an_agent_proves_it_controls_its_key_did(gateway: TestClient) -> None:  # noqa: F811
    issued = issue(gateway)
    did = issued["did"]
    first = verify(gateway, did=did, audience="Example Service")
    assert first["verified"] is True and first["found"] is True and first["reasons"] == []
    assert first["status"] == "active" and first["issuer"].startswith("did:web:")
    assert first["credential"]["signature_valid"] is True
    assert first["credential"]["status"] == "active"
    assert [owner["type"] for owner in first["controllers"]] == ["key"]
    assert "dmd_did_holder" in first["entitlements"]
    asked = first["challenge"]
    assert "To: Example Service" in asked["message"] and "proof" not in first

    second = verify(
        gateway, did=did, message=asked["message"], signature=sign_challenge(asked["message"])
    )
    assert second["verified"] is True and "challenge" not in second
    assert second["proof"]["signature_valid"] is True
    assert second["proof"]["signed_with"] == "key"
    assert second["proof"]["nonce"] == asked["nonce"]
    assert second["proof"]["audience"] == "Example Service"

    # By its identity ID too.
    assert verify(gateway, did=issued["identity_id"])["did"] == did

    stranger = encode_signature(Ed25519PrivateKey.generate().sign(asked["message"].encode()))
    for signature, reason in (
        (stranger, "not the DID-LLM owner's"),
        ("!!!", "cannot be read"),
    ):
        refused = verify(gateway, did=did, message=asked["message"], signature=signature)
        assert refused["verified"] is False and refused["proof"]["signature_valid"] is False
        assert reason in refused["reasons"][0]
    other_did = issue(gateway)["did"]
    elsewhere = verify(
        gateway,
        did=other_did,
        message=asked["message"],
        signature=sign_challenge(asked["message"]),
    )
    assert elsewhere["reasons"] == ["the message is for another DID-LLM"]
    half = gateway.post(
        "/v1/dids/verifications", json={"did": did, "message": asked["message"]}, headers=HOST
    )
    assert half.status_code == 422


@needs_db
def test_what_is_not_verified(gateway: TestClient, engine: Engine) -> None:  # noqa: F811
    unknown = verify(gateway, did="did:web:localhost%3A8000:agents:01KAAAAAAAAAAAAAAAAAAAAAAA")
    assert unknown == {
        "did": "did:web:localhost%3A8000:agents:01KAAAAAAAAAAAAAAAAAAAAAAA",
        "verified": False,
        "found": False,
        "reasons": ["not a DID-LLM issued by DebitMyData"],
        "checked_at": unknown["checked_at"],
        "controllers": [],
        "entitlements": [],
    }
    did = issue(gateway)["did"]
    elsewhere = did.replace("localhost%3A8000", "evil.example")
    assert verify(gateway, did=elsewhere)["found"] is False

    # A credential changed after it was signed.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE credentials SET payload = jsonb_set(payload, "
                "'{credentialSubject,displayName}', '\"Someone else\"') "
                "WHERE identity_id = (SELECT id FROM identities WHERE public_identifier = :did)"
            ),
            {"did": did},
        )
    changed = verify(gateway, did=did)
    assert changed["credential"]["signature_valid"] is False
    assert changed["reasons"] == ["its credential's signature is not DebitMyData's"]

    did = issue(gateway)["did"]
    code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code == 0
    suspended = verify(gateway, did=did)
    assert suspended["verified"] is False and suspended["status"] == "suspended"
    assert suspended["reasons"] == ["the DID-LLM is suspended", "its credential is suspended"]


@needs_db
def test_a_wallet_owner_proves_it_with_the_wallet(gateway: TestClient) -> None:  # noqa: F811
    did, owner = _wallet_did(gateway)
    asked = verify(gateway, did=did)["challenge"]
    proved = verify(
        gateway,
        did=did,
        message=asked["message"],
        signature=wallet_signature(owner, asked["message"]),
    )
    assert proved["verified"] is True
    assert (proved["proof"]["signed_with"], proved["proof"]["method"]) == (
        "wallet",
        "eip191_signature",
    )
    stranger = wallet_signature(Account.create(), asked["message"])
    assert (
        verify(gateway, did=did, message=asked["message"], signature=stranger)["verified"] is False
    )
    gateway.app.state.wallet_verifier = SmartWallets()  # type: ignore[attr-defined]
    smart = verify(gateway, did=did, message=asked["message"], signature="0x" + "ab" * 120)
    assert smart["verified"] is True and smart["proof"]["method"] == "erc1271_signature"


@needs_db
def test_agents_verify_each_other_over_mcp(gateway: TestClient) -> None:  # noqa: F811
    did = issue(gateway)["did"]
    call = {"name": "debitmydata_verify_agent", "arguments": {"did": did, "audience": "Agent B"}}
    first = rpc(gateway, "tools/call", call).json()["result"]["structuredContent"]
    assert first["verified"] is True
    message = first["challenge"]["message"]
    arguments = {"did": did, "message": message, "signature": sign_challenge(message)}
    second = rpc(
        gateway, "tools/call", {"name": "debitmydata_verify_agent", "arguments": arguments}
    )
    answer = second.json()["result"]["structuredContent"]
    assert answer["proof"]["signature_valid"] is True and answer["verified"] is True
