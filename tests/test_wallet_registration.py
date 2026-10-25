"""A wallet as the DID controller, against PostgreSQL, with a scripted chain (ID-06).

A quote may name an Ed25519 key, an EVM wallet, or both; each one must prove control by
signing a challenge before the DID is issued. Ordinary wallets are checked offline; smart
wallets (ERC-1271, ERC-6492) on a local chain, in the tests that need anvil.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.adapters.evm_rpc import EvmRpcClient
from dmd_gateway.identity.crypto import verify_proof
from dmd_gateway.identity.did import WALLET_CONTEXT, WALLET_METHOD
from dmd_gateway.identity.wallets import WalletVerifier
from dmd_gateway.services.staging_tools import new_test_agent_key

from .conftest import TEST_DATABASE_URL, alembic_config
from .evm_chain import ANVIL, LocalChain, local_chain
from .fakes import AGENT_CONTROLLER, SCRIPTED, sign_challenge
from .test_quotes import EXAMPLE_REQUEST, URL, _app_client, ready_registry  # noqa: F401
from .test_registration import (
    ISSUER,
    MULTIKEY,
    answer,
    audit_actions,
    challenge,
    register,
    registration,
    scalar,
    settle,
)

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

NETWORK = "eip155:8453"  # Base: the network of the approved x402 rail


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
def client(ready_registry, engine: Engine) -> Iterator[TestClient]:  # noqa: F811
    with _app_client(ready_registry, engine, issuer_key=ISSUER) as test_client:
        # No chain to ask, unless a test gives one: only ordinary wallets can prove control.
        test_client.app.state.wallet_verifier = WalletVerifier({})
        yield test_client


def wallet_quote(client: TestClient, wallet: str | None, **changes: Any) -> dict[str, Any]:
    body = {
        **EXAMPLE_REQUEST,
        "preferred_payment_option": "dmd_pow_native",
        "controller_wallet_address": wallet,
        **changes,
    }
    response = client.post(URL, json={k: v for k, v in body.items() if v is not None})
    assert response.status_code == 201, response.text
    return response.json()


def wallet_signature(account: Any, message: str) -> str:
    return "0x" + bytes(account.sign_message(encode_defunct(text=message)).signature).hex()


def challenge_for(client: TestClient, rid: str, controller_type: str) -> dict[str, Any]:
    response = client.post(
        f"/v1/dids/registrations/{rid}/controller-challenges",
        json={"controller_type": controller_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def proof_method(engine: Engine, proof_id: str) -> str:
    return str(scalar(engine, "SELECT method FROM controller_proofs WHERE id = :p", p=proof_id))


# --------------------------------------------------------------------------- ordinary wallets


def test_a_wallet_alone_controls_the_did(client: TestClient, engine: Engine) -> None:
    owner = Account.create()
    account_id = f"{NETWORK}:{owner.address}"
    q = wallet_quote(client, owner.address.lower(), controller_public_key=None)
    rid = q["registration_intent_id"]

    status = registration(client, rid)
    assert status["controller"] == {
        "public_key": None,
        "wallet_address": owner.address,  # stored checksummed
        "wallet_network": NETWORK,
        "proof_status": "pending",
        "still_to_prove": ["eip155"],
    }

    c = challenge(client, rid)
    assert c["controller"] == account_id
    assert f"Controller: {account_id}" in c["message"]
    assert c["signature_algorithm"].startswith("eip191")
    proof = answer(client, rid, c["challenge_id"], wallet_signature(owner, c["message"]))
    assert proof.status_code == 201, proof.text
    assert proof.json()["status"] == "verified"
    assert proof_method(engine, proof.json()["proof_id"]) == "eip191_signature"
    stored = scalar(
        engine, "SELECT signature FROM controller_proofs WHERE id = :p", p=proof.json()["proof_id"]
    )
    assert stored == wallet_signature(owner, c["message"])

    settle(client, q, txid=secrets.token_hex(32))
    issued = register(client, rid)
    assert issued.status_code == 201, issued.text
    body = issued.json()

    subject = body["credential"]["credentialSubject"]
    assert subject["controller"]["blockchainAccountId"] == account_id
    assert subject["controller"]["proofOfControl"] == "eip191_challenge_signature"
    assert "wallet" not in subject
    assert verify_proof(body["credential"], ISSUER.public_key_multibase)

    suffix = body["identity_id"].removeprefix("dmd_identity_")
    document = client.get(f"/agents/{suffix}/did.json").json()
    assert WALLET_CONTEXT in document["@context"]
    assert document["verificationMethod"] == [
        {
            "id": f"{body['did']}#wallet",
            "type": WALLET_METHOD,
            "controller": body["did"],
            "blockchainAccountId": account_id,
        }
    ]
    record = client.get(f"/v1/dids/{body['identity_id']}").json()
    assert record["controller_public_key"] is None
    assert record["controller_wallet"] == account_id


def test_a_key_and_a_wallet_must_both_prove_control(client: TestClient, engine: Engine) -> None:
    owner = Account.create()
    q = wallet_quote(client, owner.address)
    rid = q["registration_intent_id"]
    assert registration(client, rid)["controller"]["still_to_prove"] == ["ed25519", "eip155"]

    # By default, the challenge is for the next controller still to prove: the key first.
    key_challenge = challenge(client, rid)
    assert key_challenge["controller"] == AGENT_CONTROLLER
    key_proof = answer(
        client, rid, key_challenge["challenge_id"], sign_challenge(key_challenge["message"])
    )
    assert key_proof.status_code == 201, key_proof.text
    settle(client, q, txid=secrets.token_hex(32))

    status = registration(client, rid)
    assert status["controller"]["proof_status"] == "pending"
    assert status["controller"]["still_to_prove"] == ["eip155"]
    assert status["blockers"] == [
        "controller proof is pending, not verified (still to prove: eip155)"
    ]
    assert status["next_action"] == "prove_wallet_control"
    refused = register(client, rid)
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "registration_not_ready")

    wallet_challenge = challenge(client, rid)
    assert wallet_challenge["controller"] == f"{NETWORK}:{owner.address}"
    wallet_proof = answer(
        client,
        rid,
        wallet_challenge["challenge_id"],
        wallet_signature(owner, wallet_challenge["message"]),
    )
    assert wallet_proof.status_code == 201, wallet_proof.text
    assert wallet_proof.json()["next_action"] == "register_did"

    issued = register(client, rid)
    assert issued.status_code == 201, issued.text
    body = issued.json()
    subject = body["credential"]["credentialSubject"]
    assert subject["controller"]["publicKeyMultibase"] == MULTIKEY
    assert subject["wallet"]["blockchainAccountId"] == f"{NETWORK}:{owner.address}"
    assert verify_proof(body["credential"], ISSUER.public_key_multibase)

    suffix = body["identity_id"].removeprefix("dmd_identity_")
    methods = client.get(f"/agents/{suffix}/did.json").json()["verificationMethod"]
    assert [m["type"] for m in methods] == ["Multikey", WALLET_METHOD]

    # The identity is bound to the key's proof; the wallet's proof is in the audit trail.
    bound = scalar(
        engine, "SELECT controller_proof_id FROM identities WHERE id = :i", i=body["identity_id"]
    )
    assert bound == key_proof.json()["proof_id"]
    with engine.connect() as conn:
        data = conn.execute(
            text(
                "SELECT data FROM audit_events WHERE subject_id = :r AND action = 'identity.issued'"
            ),
            {"r": rid},
        ).scalar_one()
    assert data["wallet_proof_id"] == wallet_proof.json()["proof_id"]
    assert audit_actions(engine, rid).count("controller_proof.verified") == 2


def test_the_controller_to_prove_can_be_chosen(client: TestClient) -> None:
    owner = Account.create()
    both = wallet_quote(client, owner.address)["registration_intent_id"]
    assert challenge_for(client, both, "eip155")["controller"] == f"{NETWORK}:{owner.address}"
    assert challenge_for(client, both, "ed25519")["controller"] == AGENT_CONTROLLER

    wallet_only = wallet_quote(client, Account.create().address, controller_public_key=None)
    missing = client.post(
        f"/v1/dids/registrations/{wallet_only['registration_intent_id']}/controller-challenges",
        json={"controller_type": "ed25519"},
    )
    assert (missing.status_code, missing.json()["error"]["code"]) == (409, "no_such_controller")
    unknown = client.post(
        f"/v1/dids/registrations/{both}/controller-challenges", json={"controller_type": "rsa"}
    )
    assert unknown.status_code == 422


def test_a_wallet_signature_by_someone_else_is_refused(client: TestClient) -> None:
    owner, stranger = Account.create(), Account.create()
    rid = wallet_quote(client, owner.address, controller_public_key=None)["registration_intent_id"]

    # Signatures that are not hexadecimal leave the challenge open.
    c = challenge(client, rid)
    for malformed in (sign_challenge(c["message"]), "0x" + "zz" * 65):
        response = answer(client, rid, c["challenge_id"], malformed)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_signature_encoding"

    # Another wallet's signature: without a chain to ask, it cannot be a smart wallet's
    # either. The answer is "not supported here", and the challenge stays open.
    unsupported = answer(client, rid, c["challenge_id"], wallet_signature(stranger, c["message"]))
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "smart_wallet_not_supported"

    # The chain cannot be reached: try again later, the challenge is still open.
    client.app.state.wallet_verifier = WalletVerifier({8453: EvmRpcClient("http://127.0.0.1:9")})
    down = answer(client, rid, c["challenge_id"], wallet_signature(stranger, c["message"]))
    assert down.status_code == 503
    assert down.json()["error"]["code"] == "wallet_check_unavailable"
    assert down.headers["Retry-After"] == "30"

    right = answer(client, rid, c["challenge_id"], wallet_signature(owner, c["message"]))
    assert right.status_code == 201, right.text


def test_quotes_naming_a_wallet_do_not_block_that_wallet(
    ready_registry,  # noqa: F811
    engine: Engine,
) -> None:
    # Wallet addresses are public: if open quotes were limited per wallet, anyone could
    # block a wallet by requesting quotes for it. Only controller keys are counted.
    limit = {"max_open_quotes_per_controller": 2}
    owner = Account.create()
    with _app_client(ready_registry, engine, settings_overrides=limit) as limited:
        for spelling in (owner.address, owner.address.lower(), f"{NETWORK}:{owner.address}"):
            wallet_quote(limited, spelling, controller_public_key=None)
        # With a key, the key is counted, whatever the wallet.
        key = new_test_agent_key().controller_public_key
        for _ in range(2):
            wallet_quote(limited, owner.address, controller_public_key=key)
        refused = limited.post(
            URL,
            json={
                **EXAMPLE_REQUEST,
                "preferred_payment_option": "dmd_pow_native",
                "controller_public_key": key,
                "controller_wallet_address": Account.create().address,
            },
        )
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "too_many_open_quotes"


# --------------------------------------------------------------------------- smart wallets


@pytest.fixture(scope="module")
def chain() -> Iterator[LocalChain]:
    if ANVIL is None:
        pytest.skip("anvil (Foundry) is not installed")
    with local_chain(8453) as running:
        yield running


def test_smart_wallets_control_the_did(
    client: TestClient, engine: Engine, chain: LocalChain
) -> None:
    client.app.state.wallet_verifier = WalletVerifier({8453: EvmRpcClient(chain.url)})
    owner, stranger = Account.create(), Account.create()

    # A deployed wallet (ERC-1271): its owner signs for it.
    deployed = chain.deploy_wallet(owner.address, b"\x11" * 32)
    q = wallet_quote(client, deployed, controller_public_key=None)
    rid = q["registration_intent_id"]
    wrong = challenge(client, rid)
    rejected = answer(
        client, rid, wrong["challenge_id"], wallet_signature(stranger, wrong["message"])
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_signature"  # the challenge is used up
    c = challenge(client, rid)
    proof = answer(client, rid, c["challenge_id"], wallet_signature(owner, c["message"]))
    assert proof.status_code == 201, proof.text
    assert proof_method(engine, proof.json()["proof_id"]) == "erc1271_signature"
    settle(client, q, txid=secrets.token_hex(32))
    issued = register(client, rid)
    assert issued.status_code == 201, issued.text
    subject = issued.json()["credential"]["credentialSubject"]
    assert subject["controller"]["proofOfControl"] == "erc1271_challenge_signature"

    # A wallet that does not exist yet (ERC-6492): the signature carries its deployment.
    salt = b"\x12" * 32
    future = chain.wallet_address(owner.address, salt)
    rid = wallet_quote(client, future, controller_public_key=None)["registration_intent_id"]
    c = challenge(client, rid)
    inner = bytes.fromhex(wallet_signature(owner, c["message"])[2:])
    signature = "0x" + chain.counterfactual_signature(owner.address, salt, inner).hex()
    proof = answer(client, rid, c["challenge_id"], signature)
    assert proof.status_code == 201, proof.text
    assert proof_method(engine, proof.json()["proof_id"]) == "erc6492_signature"
    assert chain.code(future) == "0x"  # checked, not deployed
