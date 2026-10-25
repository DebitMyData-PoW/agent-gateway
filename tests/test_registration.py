"""Controller proofs and DID issuance against PostgreSQL, with a scripted chain.

Covers the issuance rule end to end: nothing is issued before the payment is settled and
the controller proof is verified, challenges are one-time and short-lived, issuance is
idempotent, and a killed rail or a missing issuer key stops issuance.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.db.session import session_factory
from dmd_gateway.identity.crypto import verify_proof
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.identity.issuer import IssuerKey
from dmd_gateway.services.settlement import run_pass

from .conftest import TEST_DATABASE_URL, alembic_config
from .fakes import AGENT_CONTROLLER, SCRIPTED, sign_challenge
from .test_quotes import ENABLED, EXAMPLE_REQUEST, URL, _app_client, ready_registry  # noqa: F401

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

DUE = 12_500_000_000  # 125 DMD at the 0.20 USD test price
BASE = "http://localhost:8000"
ISSUER = IssuerKey(Ed25519PrivateKey.generate(), issuer_did(BASE))
TXID = "ab" * 32
MULTIKEY = AGENT_CONTROLLER.removeprefix("ed25519:")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()
    command.downgrade(config, "base")


@pytest.fixture(autouse=True)
def clean_chain() -> Iterator[None]:
    SCRIPTED.reset()
    yield
    SCRIPTED.reset()


@pytest.fixture
def client(ready_registry: Path, engine: Engine) -> Iterator[TestClient]:  # noqa: F811
    with _app_client(ready_registry, engine, issuer_key=ISSUER) as test_client:
        yield test_client


# --------------------------------------------------------------------------- helpers


def quote(client: TestClient, **changes: Any) -> dict[str, Any]:
    body = {**EXAMPLE_REQUEST, "preferred_payment_option": "dmd_pow_native", **changes}
    response = client.post(URL, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def tick(client: TestClient) -> None:
    state = client.app.state
    run_pass(session_factory(state.engine), state.quote_context.adapters, datetime.now(UTC))


def settle(client: TestClient, q: dict[str, Any], txid: str = TXID) -> None:
    pid = q["payment_intent_id"]
    SCRIPTED.pay(pid, DUE, txid=txid)
    tick(client)
    SCRIPTED.confirm(pid, 6)
    tick(client)
    assert client.get(f"/v1/payments/{pid}").json()["status"] == "settled"


def challenge(client: TestClient, rid: str) -> dict[str, Any]:
    response = client.post(f"/v1/dids/registrations/{rid}/controller-challenges")
    assert response.status_code == 201, response.text
    return response.json()


def answer(client: TestClient, rid: str, challenge_id: str, signature: str):
    return client.post(
        f"/v1/dids/registrations/{rid}/controller-proofs",
        json={"challenge_id": challenge_id, "signature": signature},
    )


def prove(client: TestClient, rid: str) -> dict[str, Any]:
    c = challenge(client, rid)
    response = answer(client, rid, c["challenge_id"], sign_challenge(c["message"]))
    assert response.status_code == 201, response.text
    return response.json()


def register(client: TestClient, rid: str):
    return client.post("/v1/dids/registrations", json={"registration_intent_id": rid})


def registration(client: TestClient, rid: str) -> dict[str, Any]:
    response = client.get(f"/v1/dids/registrations/{rid}")
    assert response.status_code == 200, response.text
    return response.json()


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def audit_actions(engine: Engine, rid: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT action FROM audit_events WHERE subject_id = :s ORDER BY id"),
                {"s": rid},
            ).scalars()
        )


# --------------------------------------------------------------------------- the full flow


def test_paid_and_proven_registration_gets_its_did(client: TestClient, engine: Engine) -> None:
    q = quote(client)
    rid, pid = q["registration_intent_id"], q["payment_intent_id"]

    # Nothing is issued before payment and proof.
    status = registration(client, rid)
    assert status["next_action"] == "send_payment"
    assert status["controller"] == {
        "public_key": AGENT_CONTROLLER,
        "wallet_address": None,
        "wallet_network": None,
        "proof_status": "pending",
        "still_to_prove": ["ed25519"],
    }
    assert status["blockers"] == [
        "payment is awaiting_payment, not settled",
        "controller proof is pending, not verified",
    ]
    refused = register(client, rid)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "registration_not_ready"
    assert refused.json()["error"]["blockers"] == status["blockers"]

    # The proof can come before settlement.
    proof = prove(client, rid)
    assert proof["status"] == "verified"
    assert proof["next_action"] == "send_payment"
    assert registration(client, rid)["blockers"] == ["payment is awaiting_payment, not settled"]

    settle(client, q)
    status = registration(client, rid)
    assert status["status"] == "paid"
    assert status["blockers"] == []
    assert status["next_action"] == "register_did"

    response = register(client, rid)
    assert response.status_code == 201, response.text
    issued = response.json()
    suffix = issued["identity_id"].removeprefix("dmd_identity_")
    assert issued["did"] == f"did:web:localhost%3A8000:agents:{suffix}"
    assert issued["did_document_url"] == f"{BASE}/agents/{suffix}/did.json"
    assert issued["entitlements"] == ["dmd_did_holder"]
    assert issued["credential_url"] == f"{BASE}/v1/credentials/{issued['credential_id']}"

    # The credential is the signed receipt: payment, controller, terms, entitlements.
    credential = issued["credential"]
    subject = credential["credentialSubject"]
    assert subject["id"] == issued["did"]
    assert subject["controller"]["publicKeyMultibase"] == MULTIKEY
    payment = subject["registration"]["payment"]
    assert payment["paymentIntentId"] == pid
    assert payment["outputs"] == [f"{TXID}:0"]
    assert payment["amount"] == "125.00000000"
    assert subject["registration"]["registrationIntentId"] == rid

    # Anyone can verify it with the issuer DID document.
    issuer_doc = client.get("/.well-known/did.json")
    assert issuer_doc.headers["content-type"] == "application/did+json"
    (method,) = issuer_doc.json()["verificationMethod"]
    assert credential["proof"]["verificationMethod"] == method["id"]
    assert verify_proof(credential, method["publicKeyMultibase"])

    did_doc = client.get(f"/agents/{suffix}/did.json").json()
    assert did_doc["id"] == issued["did"]
    assert did_doc["verificationMethod"][0]["publicKeyMultibase"] == MULTIKEY

    record = client.get(f"/v1/dids/{issued['identity_id']}").json()
    assert record["status"] == "active"
    assert record["controller_public_key"] == AGENT_CONTROLLER
    assert record["credentials"] == [issued["credential_url"]]
    assert record["entitlements"] == ["dmd_did_holder"]

    stored = client.get(f"/v1/credentials/{issued['credential_id']}").json()
    assert stored["status"] == "active"
    assert stored["credential"] == credential

    entitlements = client.get(f"/v1/entitlements/{issued['identity_id']}").json()
    assert [e["entitlement"] for e in entitlements["entitlements"]] == ["dmd_did_holder"]

    # Retrying returns the same identity instead of issuing a second one.
    again = register(client, rid)
    assert again.status_code == 200
    assert again.json()["did"] == issued["did"]
    assert again.json()["credential_id"] == issued["credential_id"]
    issued_count = "SELECT count(*) FROM identities WHERE registration_intent_id = :r"
    assert scalar(engine, issued_count, r=rid) == 1

    status = registration(client, rid)
    assert (status["status"], status["next_action"], status["did"]) == (
        "issued",
        "none",
        issued["did"],
    )
    assert status["blockers"] == []
    assert client.post(f"/v1/dids/registrations/{rid}/controller-challenges").status_code == 409

    # The identity is bound to the proof, and every step is in the audit trail.
    bound_proof = (
        "SELECT p.status FROM identities i JOIN controller_proofs p"
        " ON p.id = i.controller_proof_id WHERE i.id = :i"
    )
    assert scalar(engine, bound_proof, i=issued["identity_id"]) == "verified"
    consumed = "SELECT consumed_at IS NOT NULL FROM registration_intents WHERE id = :r"
    assert scalar(engine, consumed, r=rid)
    actions = audit_actions(engine, rid)
    for action in (
        "registration_quote.created",
        "controller_challenge.created",
        "controller_proof.verified",
        "identity.issued",
    ):
        assert action in actions
    assert audit_actions(engine, issued["identity_id"]) == [
        "credential.issued",
        "entitlement.granted",
    ]


def test_reorg_after_issuance_raises_an_alert(client: TestClient, engine: Engine) -> None:
    q = quote(client)
    rid = q["registration_intent_id"]
    prove(client, rid)
    settle(client, q, txid="cd" * 32)
    assert register(client, rid).status_code == 201

    SCRIPTED.confirm(q["payment_intent_id"], 0)  # the block with the payment is gone
    tick(client)
    assert "alert.issued_identity_payment_reorged" in audit_actions(engine, rid)
    # The issued DID stays issued; an operator decides what happens next.
    assert registration(client, rid)["status"] == "issued"


# --------------------------------------------------------------------------- challenges


def test_wrong_signature_is_rejected_and_uses_up_the_challenge(
    client: TestClient, engine: Engine
) -> None:
    rid = quote(client)["registration_intent_id"]
    c = challenge(client, rid)
    assert c["controller"] == AGENT_CONTROLLER
    assert c["message"].startswith("DebitMyData controller proof\nAudience: http://localhost:8000")
    assert f"Registration: {rid}" in c["message"]

    other_key = Ed25519PrivateKey.generate()
    wrong = answer(client, rid, c["challenge_id"], sign_challenge(c["message"], other_key))
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "invalid_signature"

    # The challenge cannot be retried, even with the right signature.
    replay = answer(client, rid, c["challenge_id"], sign_challenge(c["message"]))
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "challenge_already_used"

    assert scalar(
        engine,
        "SELECT failure_reason FROM controller_proofs WHERE challenge_id = :c",
        c=c["challenge_id"],
    ) == ("the signature does not match the controller public key")
    assert registration(client, rid)["controller"]["proof_status"] == "pending"
    assert "controller_proof.rejected" in audit_actions(engine, rid)


def test_signature_over_a_different_message_is_rejected(client: TestClient) -> None:
    rid = quote(client)["registration_intent_id"]
    c = challenge(client, rid)
    forged = sign_challenge(c["message"].replace("Purpose: did_registration", "Purpose: other"))
    assert answer(client, rid, c["challenge_id"], forged).status_code == 422


def test_malformed_signature_keeps_the_challenge_open(client: TestClient) -> None:
    rid = quote(client)["registration_intent_id"]
    c = challenge(client, rid)
    bad = answer(client, rid, c["challenge_id"], "a" * 90)
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_signature_encoding"
    good = answer(client, rid, c["challenge_id"], sign_challenge(c["message"]))
    assert good.status_code == 201


def test_expired_challenge_is_refused(client: TestClient, engine: Engine) -> None:
    rid = quote(client)["registration_intent_id"]
    c = challenge(client, rid)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE controller_challenges SET expires_at = now() - interval '1 second'"
                " WHERE id = :c"
            ),
            {"c": c["challenge_id"]},
        )
    response = answer(client, rid, c["challenge_id"], sign_challenge(c["message"]))
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "challenge_expired"


def test_challenge_only_works_for_its_own_registration(client: TestClient) -> None:
    first = quote(client)["registration_intent_id"]
    second = quote(client)["registration_intent_id"]
    c = challenge(client, first)
    response = answer(client, second, c["challenge_id"], sign_challenge(c["message"]))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_challenge"


def test_open_challenges_are_limited(client: TestClient) -> None:
    rid = quote(client)["registration_intent_id"]
    for _ in range(5):
        challenge(client, rid)
    response = client.post(f"/v1/dids/registrations/{rid}/controller-challenges")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"


def test_unknown_and_malformed_ids(client: TestClient) -> None:
    unknown = "dmd_did_intent_" + "0" * 26
    assert client.get(f"/v1/dids/registrations/{unknown}").status_code == 404
    assert client.post(f"/v1/dids/registrations/{unknown}/controller-challenges").status_code == 404
    assert register(client, unknown).status_code == 404
    assert client.get("/v1/dids/registrations/not-an-id").status_code == 422
    assert client.get("/v1/dids/dmd_identity_" + "0" * 26).status_code == 404
    assert client.get("/v1/credentials/dmd_credential_" + "0" * 26).status_code == 404
    assert client.get("/agents/" + "0" * 26 + "/did.json").status_code == 404


# --------------------------------------------------------------------------- policy


def test_issuance_needs_an_issuer_key(ready_registry: Path, engine: Engine) -> None:  # noqa: F811
    with _app_client(ready_registry, engine) as client:
        q = quote(client)
        rid = q["registration_intent_id"]
        prove(client, rid)
        settle(client, q, txid="ef" * 32)
        status = registration(client, rid)
        assert status["blockers"] == ["DID issuance is not enabled on this gateway yet"]
        response = register(client, rid)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "issuance_unavailable"
        assert client.get("/.well-known/did.json").status_code == 404
        offer = client.get("/v1/discovery").json()["offers"][0]
        assert offer["endpoints"]["registration"]["available"] is False
        assert offer["endpoints"]["quote"]["available"] is True


def test_killed_rail_stops_issuance(
    client: TestClient,
    ready_registry: Path,  # noqa: F811
    engine: Engine,
) -> None:
    q = quote(client)
    rid = q["registration_intent_id"]
    prove(client, rid)
    settle(client, q, txid="12" * 32)

    killed = {**ENABLED, "DMD_RAIL_DMD_POW_NATIVE_KILL": "1"}
    with _app_client(ready_registry, engine, environ=killed, issuer_key=ISSUER) as stopped:
        response = register(stopped, rid)
        assert response.status_code == 409
        assert response.json()["error"]["blockers"] == [
            "payment option dmd_pow_native is currently disabled"
        ]
        assert registration(stopped, rid)["next_action"] == "contact_support"

    # Once the rail is back, the same registration completes.
    assert register(client, rid).status_code == 201


def test_discovery_advertises_the_issuer(client: TestClient) -> None:
    body = client.get("/v1/discovery").json()
    assert body["credential_issuer"] == {
        "did": "did:web:localhost%3A8000",
        "did_document_url": f"{BASE}/.well-known/did.json",
    }
    assert body["offers"][0]["endpoints"]["registration"]["available"] is True


def test_proofs_cannot_be_altered(client: TestClient, engine: Engine) -> None:
    rid = quote(client)["registration_intent_id"]
    prove(client, rid)
    with pytest.raises(Exception, match="append-only"), engine.begin() as conn:
        conn.execute(text("UPDATE controller_proofs SET status = 'rejected'"))
