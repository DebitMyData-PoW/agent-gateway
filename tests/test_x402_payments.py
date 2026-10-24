"""Paying a DID registration with x402 (USDC on Base), against PostgreSQL.

The gateway runs with its real x402 adapter, connected to an in-memory chain and a
facilitator that checks signatures like a real one (``tests/x402_fakes.py``). Covers: the
402 answer and its contents, payment only once the DID can be issued, the checks on the
payment, settlement checked on chain before anything is credited, waiting for
confirmations, failures that never charge twice, MCP, and the staging test payer.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from alembic import command
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.db.session import session_factory
from dmd_gateway.identity.crypto import verify_proof
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.identity.issuer import IssuerKey
from dmd_gateway.main import create_app
from dmd_gateway.services.settlement import process_intent, run_pass
from dmd_gateway.services.x402_payments import encode_header
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL, alembic_config
from .fakes import AGENT_CONTROLLER, FakeDmdAdapter, fixed_price_factory, sign_challenge
from .x402_fakes import (
    CHAIN,
    FACILITATOR,
    MERCHANT,
    NETWORK,
    TOKEN,
    TestPayer,
    TestX402Adapter,
    recover_payer,
    reset_chain,
)

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

BASE = "http://localhost:8000"
ISSUER = IssuerKey(Ed25519PrivateKey.generate(), issuer_did(BASE))
QUOTES = "/v1/dids/registration-quotes"
REGISTER = "/v1/dids/registrations"
PRICE_SOURCE = "approved_dmd_usd_source"
ENVIRON = {
    "DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true",
    "DMD_RAIL_X402_USDC_BASE_ENABLED": "true",
    "DMD_USDC_BASE_MERCHANT_ADDRESS": MERCHANT,
    "DMD_X402_FACILITATOR_URL": "https://facilitator.test",
    "DMD_BASE_RPC_URL": "https://rpc.test",
}
ADAPTERS = {"DMDNativePaymentAdapter": FakeDmdAdapter, "X402PaymentAdapter": TestX402Adapter}
DUE = 25_000_000  # 25.00 USDC


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()
    command.downgrade(config, "base")


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    data["rails"]["dmd_pow_native"]["confirmation_policy"]["required_confirmations"] = 6
    data["rails"]["dmd_pow_native"]["price_policy"] = {
        "source": PRICE_SOURCE,
        "max_price_age_seconds": 300,
        "max_deviation_bps": 200,
        "fallback": "refuse_quote",
    }
    data["rails"]["x402_usdc_base"]["asset"]["canonical_id"] = f"{NETWORK}/erc20:{TOKEN}"
    path = tmp_path_factory.mktemp("x402") / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def make_app(
    registry: Path,
    engine: Engine,
    *,
    wait: float = 0,
    issuer: IssuerKey | None = ISSUER,
    environ: dict[str, str] | None = None,
    **settings: Any,
) -> FastAPI:
    return create_app(
        Settings(
            _env_file=None,
            database_url=TEST_DATABASE_URL,
            route_registry_path=registry,
            x402_settlement_wait_seconds=wait,
            **settings,
        ),
        environ=environ or ENVIRON,
        engine=engine,
        adapters=ADAPTERS,
        price_sources={PRICE_SOURCE: fixed_price_factory(PRICE_SOURCE, "0.2")},
        issuer_key=issuer,
    )


@pytest.fixture(autouse=True)
def fresh_chain() -> Iterator[None]:
    reset_chain()
    yield
    reset_chain()


@pytest.fixture
def client(registry: Path, engine: Engine) -> Iterator[TestClient]:
    with TestClient(make_app(registry, engine)) as test_client:
        yield test_client


# --------------------------------------------------------------------------- helpers


def quote(client: TestClient) -> dict[str, Any]:
    response = client.post(
        QUOTES,
        json={
            "display_name": "BaseAgent-01",
            "participant_type": "autonomous_agent",
            "controller_public_key": AGENT_CONTROLLER,
            "preferred_payment_option": "x402_usdc_base",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def prove(client: TestClient, rid: str) -> None:
    challenge = client.post(f"{REGISTER}/{rid}/controller-challenges").json()
    response = client.post(
        f"{REGISTER}/{rid}/controller-proofs",
        json={
            "challenge_id": challenge["challenge_id"],
            "signature": sign_challenge(challenge["message"]),
        },
    )
    assert response.status_code == 201, response.text


def register(client: TestClient, rid: str, header: str | None = None) -> httpx2.Response:
    headers = {"PAYMENT-SIGNATURE": header} if header else {}
    return client.post(REGISTER, json={"registration_intent_id": rid}, headers=headers)


def decode(value: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(value))


def payment_required(client: TestClient, rid: str) -> dict[str, Any]:
    response = register(client, rid)
    assert response.status_code == 402, response.text
    return decode(response.headers["PAYMENT-REQUIRED"])


def ready(client: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """A quote whose controller is proven, and its PaymentRequired."""
    q = quote(client)
    prove(client, q["registration_intent_id"])
    return q, payment_required(client, q["registration_intent_id"])


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def authorization_statuses(engine: Engine, pid: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status FROM x402_authorizations WHERE payment_intent_id = :p ORDER BY id"),
            {"p": pid},
        )
        return [row[0] for row in rows]


# --------------------------------------------------------------------------- the 402


def test_payment_is_asked_only_once_the_did_can_be_issued(client: TestClient) -> None:
    q = quote(client)
    rid = q["registration_intent_id"]
    assert q["next_action"] == "prove_wallet_control"
    assert q["selected_payment_option"]["recipient"] == MERCHANT

    # Controller not proven yet: no 402, nothing to pay.
    response = register(client, rid, TestPayer().header(_any_required(client)))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "registration_not_ready"
    assert FACILITATOR.verify_calls == []

    prove(client, rid)
    status = client.get(f"{REGISTER}/{rid}").json()
    assert status["next_action"] == "register_did_with_x402_payment"
    payment = client.get(f"/v1/payments/{q['payment_intent_id']}").json()
    assert payment["next_action"] == "register_did_with_x402_payment"

    response = register(client, rid)
    assert response.status_code == 402
    body = response.json()["error"]
    assert body["code"] == "payment_required"
    required = decode(response.headers["PAYMENT-REQUIRED"])
    assert body["payment_required"] == required
    assert required["x402Version"] == 2
    assert required["resource"]["url"] == f"{BASE}{REGISTER}"
    assert required["resource"]["serviceName"] == "DebitMyData"
    (accepts,) = required["accepts"]
    assert accepts["scheme"] == "exact"
    assert accepts["network"] == NETWORK
    assert accepts["amount"] == str(DUE)
    assert accepts["asset"] == TOKEN
    assert accepts["payTo"] == MERCHANT
    assert accepts["extra"] == {"name": "USD Coin", "version": "2"}
    assert 1 <= accepts["maxTimeoutSeconds"] <= 300
    bazaar = required["extensions"]["bazaar"]
    assert bazaar["info"]["input"]["method"] == "POST"
    assert bazaar["info"]["input"]["body"].keys() == {"registration_intent_id"}
    assert bazaar["schema"]["required"] == ["input"]


def _any_required(client: TestClient) -> dict[str, Any]:
    """A PaymentRequired from another, proven registration (to sign something valid)."""
    _, required = ready(client)
    return required


def test_paid_registration_issues_the_did_in_the_same_answer(
    client: TestClient, engine: Engine
) -> None:
    q, required = ready(client)
    rid, pid = q["registration_intent_id"], q["payment_intent_id"]
    payer = TestPayer()

    response = register(client, rid, payer.header(required))
    assert response.status_code == 201, response.text
    issued = response.json()
    assert issued["did"].startswith("did:web:localhost%3A8000:agents:")
    assert verify_proof(issued["credential"], ISSUER.public_key_multibase)
    settlement = decode(response.headers["PAYMENT-RESPONSE"])
    assert settlement["success"] is True
    assert settlement["network"] == NETWORK
    assert settlement["payer"] == payer.address.lower()
    assert issued["payment_response"] == settlement

    # Credited from the chain: the transfer's transaction and log index, exactly once.
    tx = settlement["transaction"]
    assert (
        scalar(
            engine, "SELECT output_index FROM credited_outputs WHERE payment_intent_id = :p", p=pid
        )
        == 1
    )
    assert (
        scalar(
            engine,
            "SELECT transaction_reference FROM credited_outputs WHERE payment_intent_id = :p",
            p=pid,
        )
        == tx
    )
    assert CHAIN.balances[MERCHANT.lower()] == DUE
    assert client.get(f"/v1/payments/{pid}").json()["status"] == "settled"
    assert authorization_statuses(engine, pid) == ["settled"]
    with engine.connect() as conn:
        actions = [
            row[0]
            for row in conn.execute(
                text("SELECT action FROM audit_events WHERE subject_id = :p ORDER BY id"),
                {"p": pid},
            )
        ]
    assert [a for a in actions if a.startswith("payment.x402")] == [
        "payment.x402_authorization_received",
        "payment.x402_submitted",
        "payment.x402_settled",
    ]
    assert "payment.settled" in actions

    # Asking again returns the same DID and never pays again, even with a new payment.
    again = register(client, rid, payer.header(required))
    assert again.status_code == 200
    assert again.json()["did"] == issued["did"]
    assert len(FACILITATOR.settle_calls) == 1


def test_confirmations_still_coming_answer_202_then_the_did(
    client: TestClient, engine: Engine
) -> None:
    CHAIN.auto_confirm = 0
    q, required = ready(client)
    rid = q["registration_intent_id"]

    response = register(client, rid, TestPayer().header(required))
    assert response.status_code == 202, response.text
    assert response.headers["Retry-After"] == "10"
    assert decode(response.headers["PAYMENT-RESPONSE"])["success"] is True
    body = response.json()
    assert body["status"] == "payment_confirming"
    assert body["required_confirmations"] == 5
    assert body["next_action"] == "register_did"

    # Not enough confirmations yet: still 202, and no second settlement.
    assert register(client, rid).status_code == 202
    CHAIN.mine(5)
    done = register(client, rid)
    assert done.status_code == 201, done.text
    assert len(FACILITATOR.settle_calls) == 1


# --------------------------------------------------------------------------- refused payments


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"value": DUE - 1}, "invalid_exact_evm_payload_authorization_value_mismatch"),
        ({"to": "0x" + "33" * 20}, "invalid_exact_evm_payload_recipient_mismatch"),
        ({"validBefore": 1}, "invalid_exact_evm_payload_authorization_valid_before"),
        ({"validAfter": 4_000_000_000}, "invalid_exact_evm_payload_authorization_valid_after"),
        # Usable after the quote expires: it could be settled when the quote no longer stands.
        ({"validBefore": 4_000_000_000}, "invalid_exact_evm_payload_authorization_valid_before"),
    ],
)
def test_a_payment_that_does_not_match_the_quote_is_refused_before_anything_else(
    client: TestClient, engine: Engine, changes: dict[str, Any], reason: str
) -> None:
    q, required = ready(client)
    response = register(
        client, q["registration_intent_id"], TestPayer().header(required, **changes)
    )
    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "payment_mismatch"
    assert reason in error["reasons"]
    assert "PAYMENT-REQUIRED" in response.headers
    assert FACILITATOR.verify_calls == [] and FACILITATOR.settle_calls == []
    assert authorization_statuses(engine, q["payment_intent_id"]) == []


def test_requirements_other_than_the_quote_are_refused(client: TestClient) -> None:
    q, required = ready(client)
    other = json.loads(json.dumps(required))
    other["accepts"][0]["amount"] = "1"
    payload = TestPayer().payload(other, value=DUE)
    response = register(client, q["registration_intent_id"], encode_header(payload))
    assert response.status_code == 402
    assert "invalid_payment_requirements" in response.json()["error"]["reasons"]


@pytest.mark.parametrize("header", ["not base64!", "e30", base64.b64encode(b"[1,2]").decode()])
def test_malformed_payment_header_is_a_bad_request(client: TestClient, header: str) -> None:
    q, _ = ready(client)
    response = register(client, q["registration_intent_id"], header)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_payment"
    assert "PAYMENT-REQUIRED" in response.headers


def test_facilitator_refusal_charges_nothing_and_a_new_payment_works(
    client: TestClient, engine: Engine
) -> None:
    q, required = ready(client)
    rid, pid = q["registration_intent_id"], q["payment_intent_id"]
    poor = TestPayer(balance=1)
    response = register(client, rid, poor.header(required))
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payment_invalid"
    assert "insufficient_funds" in response.json()["error"]["message"]
    assert FACILITATOR.settle_calls == []
    assert authorization_statuses(engine, pid) == ["verify_rejected"]

    response = register(client, rid, TestPayer().header(required))
    assert response.status_code == 201, response.text
    assert authorization_statuses(engine, pid) == ["verify_rejected", "settled"]


def test_a_forged_signature_is_refused(client: TestClient) -> None:
    q, required = ready(client)
    payload = TestPayer().payload(required)
    payload["payload"]["authorization"]["from"] = TestPayer().address  # signed by another key
    response = register(client, q["registration_intent_id"], encode_header(payload))
    assert response.status_code == 402
    assert "invalid_exact_evm_payload_signature" in response.json()["error"]["message"]


def test_failed_settlement_charges_nothing(client: TestClient, engine: Engine) -> None:
    q, required = ready(client)
    FACILITATOR.settle_mode = "fail"
    response = register(client, q["registration_intent_id"], TestPayer().header(required))
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payment_failed"
    assert authorization_statuses(engine, q["payment_intent_id"]) == ["settle_rejected"]
    assert MERCHANT.lower() not in CHAIN.balances


def test_lost_facilitator_answer_is_recovered_from_the_chain(
    client: TestClient, engine: Engine
) -> None:
    """The facilitator broadcast the payment but its answer never came back."""
    q, required = ready(client)
    rid, pid = q["registration_intent_id"], q["payment_intent_id"]
    FACILITATOR.settle_mode = "down_after_broadcast"
    response = register(client, rid, TestPayer().header(required))
    # Found through the token's AuthorizationUsed event for this payer and nonce.
    assert response.status_code == 201, response.text
    assert "eth_getLogs" in CHAIN.calls
    assert authorization_statuses(engine, pid) == ["submitted"]
    assert client.get(f"/v1/payments/{pid}").json()["status"] == "settled"


def test_unknown_settlement_outcome_waits_and_never_pays_twice(
    client: TestClient, engine: Engine
) -> None:
    q, required = ready(client)
    rid, pid = q["registration_intent_id"], q["payment_intent_id"]
    FACILITATOR.settle_mode = "down"
    response = register(client, rid, TestPayer().header(required))
    assert response.status_code == 202
    assert "PAYMENT-RESPONSE" not in response.headers
    # A second payment while the first one's outcome is unknown is not used.
    FACILITATOR.settle_mode = "ok"
    response = register(client, rid, TestPayer().header(required))
    assert response.status_code == 202
    assert len(FACILITATOR.settle_calls) == 1
    assert authorization_statuses(engine, pid) == ["submitted"]


def test_one_authorization_cannot_pay_two_registrations(client: TestClient) -> None:
    q1, required1 = ready(client)
    q2, required2 = ready(client)
    payer = TestPayer()
    nonce = "0x" + "77" * 32
    assert (
        register(
            client, q1["registration_intent_id"], payer.header(required1, nonce=nonce)
        ).status_code
        == 201
    )
    response = register(client, q2["registration_intent_id"], payer.header(required2, nonce=nonce))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "authorization_already_used"


def test_no_payment_in_the_last_moments_of_a_quote(client: TestClient, engine: Engine) -> None:
    q, _ = ready(client)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE payment_intents SET quote_expires_at = :t WHERE id = :p"),
            {"t": datetime.now(UTC) + timedelta(seconds=20), "p": q["payment_intent_id"]},
        )
    response = register(client, q["registration_intent_id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "quote_expiring"


def test_no_payment_is_asked_when_issuance_is_off(registry: Path, engine: Engine) -> None:
    with TestClient(make_app(registry, engine, issuer=None)) as client:
        q = quote(client)
        prove(client, q["registration_intent_id"])
        response = register(client, q["registration_intent_id"])
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "issuance_unavailable"


def test_references_are_not_used_for_x402(client: TestClient) -> None:
    q = quote(client)
    response = client.post(
        f"/v1/payments/{q['payment_intent_id']}/references",
        json={"transaction_reference": "0x" + "ab" * 32},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_used_for_x402"


# --------------------------------------------------------------------------- worker and chain


def tick(app: FastAPI) -> None:
    state = app.state
    run_pass(session_factory(state.engine), state.quote_context.adapters, datetime.now(UTC))


def test_worker_reads_nothing_for_unpaid_x402_quotes(client: TestClient) -> None:
    q = quote(client)
    CHAIN.calls.clear()
    state = client.app.state
    with session_factory(state.engine)() as session, session.begin():
        process_intent(
            session, q["payment_intent_id"], state.quote_context.adapters, datetime.now(UTC)
        )
    assert CHAIN.calls == []  # nothing was submitted, so there is nothing to look for


def test_reorg_reopens_the_payment_until_it_is_mined_again(
    client: TestClient, engine: Engine
) -> None:
    CHAIN.auto_confirm = 0
    q, required = ready(client)
    rid, pid = q["registration_intent_id"], q["payment_intent_id"]
    response = register(client, rid, TestPayer().header(required))
    assert response.status_code == 202
    tx = decode(response.headers["PAYMENT-RESPONSE"])["transaction"]

    CHAIN.reorg()  # the block holding the payment left the chain
    tick(client.app)
    assert client.get(f"/v1/payments/{pid}").json()["status"] in ("reorged", "awaiting_payment")
    assert register(client, rid).status_code == 202

    CHAIN.auto_confirm = 5
    CHAIN.remine(tx)
    response = register(client, rid)
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- MCP


@asynccontextmanager
async def mcp_client(app: FastAPI) -> AsyncIterator[Client]:
    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)
        async with http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
            yield client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("way", ["argument", "meta"])
async def test_paying_through_mcp(registry: Path, engine: Engine, way: str) -> None:
    app = make_app(registry, engine)
    q, _ = ready(TestClient(app))  # REST without starting the app; MCP starts it below
    rid = q["registration_intent_id"]
    async with mcp_client(app) as mcp:
        refused = await mcp.call_tool("dmd_register_did", {"registration_intent_id": rid})
        assert refused.is_error
        required = json.loads(refused.content[0].text)  # the x402 PaymentRequired itself
        assert refused.structured_content == required
        assert required["x402Version"] == 2 and required["accepts"][0]["amount"] == str(DUE)

        payload = TestPayer().payload(required)
        if way == "argument":
            result = await mcp.call_tool(
                "dmd_register_did", {"registration_intent_id": rid, "x402_payment": payload}
            )
        else:
            result = await mcp.call_tool(
                "dmd_register_did",
                {"registration_intent_id": rid},
                meta={"x402/payment": payload},
            )
    assert not result.is_error, result.content[0].text
    issued = result.structured_content
    assert issued["status"] == "issued"
    assert issued["payment_response"]["success"] is True
    assert result.meta["x402/payment-response"] == issued["payment_response"]


# --------------------------------------------------------------------------- staging


def test_staging_test_payer_signs_payments_the_registration_accepts(
    registry: Path, engine: Engine, tmp_path: Path
) -> None:
    payer = TestPayer()
    environ = {**ENVIRON, "DMD_STAGING_BASE_PAYER_KEY": payer.account.key.hex()}

    # Quotes on a real network are never signed by the test wallet.
    app = make_app(registry, engine, environ=environ, staging_test_tools=True)
    with TestClient(app) as client:
        q = quote(client)
        response = client.post(
            "/v1/staging/test-payments", json={"payment_intent_id": q["payment_intent_id"]}
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_a_test_payment"

    data = yaml.safe_load(registry.read_text(encoding="utf-8"))
    usdc = data["rails"]["x402_usdc_base"]
    usdc["network"] = "eip155:84532"
    usdc["asset"]["canonical_id"] = f"eip155:84532/erc20:{TOKEN}"
    sepolia = tmp_path / "sepolia.yaml"
    sepolia.write_text(yaml.safe_dump(data), encoding="utf-8")
    CHAIN.chain_id = 84532

    app = make_app(sepolia, engine, environ=environ, staging_test_tools=True)
    with TestClient(app) as client:
        q, required = ready(client)
        response = client.post(
            "/v1/staging/test-payments", json={"payment_intent_id": q["payment_intent_id"]}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["payer"] == payer.address
        assert body["transaction_reference"] is None
        payload = decode(body["payment_signature"])
        for key in ("network", "asset", "payTo", "amount", "extra"):
            assert payload["accepted"][key] == required["accepts"][0][key]
        assert recover_payer(payload, payload["accepted"]) == payer.address
        assert payload["extensions"]["bazaar"] == required["extensions"]["bazaar"]

        issued = register(client, q["registration_intent_id"], body["payment_signature"])
    assert issued.status_code == 201, issued.text
    assert decode(issued.headers["PAYMENT-RESPONSE"])["network"] == "eip155:84532"


def test_a_payment_taken_over_by_another_request_is_never_settled(
    client: TestClient, engine: Engine
) -> None:
    """If an authorization was set aside while it was being verified, settling stops."""
    q, required = ready(client)
    pid = q["payment_intent_id"]

    def set_aside(payload: dict[str, Any], requirements: dict[str, Any]) -> Any:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE x402_authorizations SET status = 'verify_rejected' "
                    "WHERE payment_intent_id = :p"
                ),
                {"p": pid},
            )
        return type(FACILITATOR).verify(FACILITATOR, payload, requirements)

    FACILITATOR.verify = set_aside  # type: ignore[method-assign]
    try:
        response = register(client, q["registration_intent_id"], TestPayer().header(required))
    finally:
        del FACILITATOR.verify
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_in_progress"
    assert FACILITATOR.settle_calls == []


def test_refused_payments_are_limited_per_quote(client: TestClient, engine: Engine) -> None:
    q, required = ready(client)
    rid = q["registration_intent_id"]
    FACILITATOR.refuse_verify = "invalid_exact_evm_payload_signature"
    for _ in range(10):
        assert register(client, rid, TestPayer().header(required)).status_code == 402
    response = register(client, rid, TestPayer().header(required))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_payment_attempts"
    assert len(FACILITATOR.verify_calls) == 10
