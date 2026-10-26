"""Inference donations paid with x402 (USDC on Base), against PostgreSQL.

Agents donate towards DebitMyData's AI inference in an amount they choose, in one call, with
the same x402 adapter, facilitator and on-chain checks as DID payments (``tests/x402_fakes``
stands in for Base and the facilitator). Covers: the 402 and its terms, probes that can
never be paid, the amount range, the signed receipt, replays that never pay twice, refused
and slow payments, the receiving wallet, discovery, MCP and the staging test payer.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from alembic import command
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.db.session import session_factory
from dmd_gateway.domain.routes import RegistryError
from dmd_gateway.identity.crypto import verify_proof
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.identity.issuer import IssuerKey
from dmd_gateway.main import create_app
from dmd_gateway.services.settlement import run_pass
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL, alembic_config
from .fakes import FakeDmdAdapter, fixed_price_factory
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
DONATE = "/v1/donations/inference"
INFERENCE = "0x3BA9A001ec76Dc24384f8dEE8bE3479cE45bD42E"  # a wallet of its own, not MERCHANT
PRICE_SOURCE = "approved_dmd_usd_source"
ENVIRON = {
    "DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true",
    "DMD_RAIL_X402_USDC_BASE_ENABLED": "true",
    "DMD_USDC_BASE_MERCHANT_ADDRESS": MERCHANT,
    "DMD_USDC_BASE_INFERENCE_ADDRESS": INFERENCE,
    "DMD_X402_FACILITATOR_URL": "https://facilitator.test",
    "DMD_BASE_RPC_URL": "https://rpc.test",
}
ADAPTERS = {"DMDNativePaymentAdapter": FakeDmdAdapter, "X402PaymentAdapter": TestX402Adapter}
FIVE = 5_000_000  # 5.00 USDC


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
    data["rails"]["dmd_pow_native"]["price_policy"] = {
        "source": PRICE_SOURCE,
        "max_price_age_seconds": 300,
        "max_deviation_bps": 200,
        "fallback": "refuse_quote",
    }
    data["rails"]["x402_usdc_base"]["asset"]["canonical_id"] = f"{NETWORK}/erc20:{TOKEN}"
    path = tmp_path_factory.mktemp("donations") / "routes.yaml"
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
        environ=ENVIRON if environ is None else environ,
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


def decode(value: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(value))


def donate(
    client: TestClient, body: dict[str, Any] | None, header: str | None = None
) -> httpx2.Response:
    headers = {"PAYMENT-SIGNATURE": header} if header else {}
    if body is None:
        return client.post(DONATE, headers=headers)
    return client.post(DONATE, json=body, headers=headers)


def required_for(client: TestClient, amount: str = "5.00") -> dict[str, Any]:
    response = donate(client, {"amount_usd": amount})
    assert response.status_code == 402, response.text
    assert response.json()["error"]["code"] == "payment_required"
    return decode(response.headers["PAYMENT-REQUIRED"])


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def tick(app: FastAPI) -> None:
    state = app.state
    run_pass(session_factory(state.engine), state.quote_context.adapters, datetime.now(UTC))


def actions(engine: Engine, subject_id: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT action FROM audit_events WHERE subject_id = :s ORDER BY id"),
            {"s": subject_id},
        )
        return [row[0] for row in rows]


# --------------------------------------------------------------------------- the 402


def test_the_402_asks_for_the_chosen_amount_in_usdc_to_the_inference_wallet(
    client: TestClient,
) -> None:
    response = donate(client, {"amount_usd": "5"})
    assert response.status_code == 402
    body = response.json()
    assert body["error"]["code"] == "payment_required"
    required = decode(response.headers["PAYMENT-REQUIRED"])
    assert body["error"]["payment_required"] == required
    for key in ("x402Version", "resource", "accepts", "extensions"):
        assert body[key] == required[key]
    assert required["x402Version"] == 2
    assert required["resource"]["url"] == f"{BASE}{DONATE}"
    assert required["resource"]["serviceName"] == "DebitMyData"
    (accepts,) = required["accepts"]
    assert accepts["scheme"] == "exact"
    assert (accepts["network"], accepts["asset"], accepts["amount"]) == (NETWORK, TOKEN, str(FIVE))
    assert accepts["payTo"] == INFERENCE  # the donation wallet, never the DID payment wallet
    assert accepts["extra"] == {"name": "USD Coin", "version": "2"}
    assert 1 <= accepts["maxTimeoutSeconds"] <= 300
    assert FACILITATOR.verify_calls == []


def test_bazaar_metadata_describes_the_donation(client: TestClient) -> None:
    required = required_for(client)
    description = required["resource"]["description"]
    assert len(description) <= 500
    bazaar = required["extensions"]["bazaar"]
    Draft202012Validator.check_schema(bazaar["schema"])
    input_schema = bazaar["schema"]["properties"]["input"]
    assert not list(Draft202012Validator(input_schema).iter_errors(bazaar["info"]["input"]))
    for body in ({}, {"amount_usd": "five"}, {"amount_usd": "1.00", "x": 1}):
        candidate = {**bazaar["info"]["input"], "body": body}
        assert list(Draft202012Validator(input_schema).iter_errors(candidate))


@pytest.mark.parametrize("body", [None, {}, {"donor_name": "Probe"}])
def test_probes_without_an_amount_get_example_terms_and_can_never_be_paid(
    client: TestClient, engine: Engine, body: dict[str, Any] | None
) -> None:
    """Coinbase's validator posts an empty body first, then the advertised example."""
    purchases = scalar(engine, "SELECT count(*) FROM purchases")
    response = donate(client, body)
    assert response.status_code == 402, response.text
    assert response.json()["error"]["code"] == "donation_amount_required"
    required = decode(response.headers["PAYMENT-REQUIRED"])
    (accepts,) = required["accepts"]
    assert (accepts["amount"], accepts["payTo"]) == ("1000000", INFERENCE)
    assert required["extensions"]["bazaar"]["info"]["input"]["body"] == {"amount_usd": "1.00"}

    payer = TestPayer()
    response = donate(client, body, payer.header(required))
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "donation_amount_required"
    assert FACILITATOR.verify_calls == [] and FACILITATOR.settle_calls == []
    assert CHAIN.balances[payer.address.lower()] == 100_000_000
    assert scalar(engine, "SELECT count(*) FROM purchases") == purchases


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"amount_usd": "0.49"}, "invalid_amount"),
        ({"amount_usd": "1000.01"}, "invalid_amount"),
        ({"amount_usd": "0"}, "invalid_amount"),
        ({"amount_usd": "1.234"}, None),
        ({"amount_usd": "-1"}, None),
        ({"amount_usd": 5}, None),
        ({"amount_usd": "5.00", "donor_did": "not a did"}, None),
        ({"amount_usd": "5.00", "donor_name": "x" * 81}, None),
        ({"amount_usd": "5.00", "other": 1}, None),
    ],
)
def test_bad_input_is_refused_before_any_payment(
    client: TestClient, body: dict[str, Any], code: str | None
) -> None:
    response = donate(client, body, TestPayer().header(required_for(client)))
    assert response.status_code == 422, response.text
    if code:
        assert response.json()["error"]["code"] == code
        error = response.json()["error"]
        assert (error["minimum"], error["maximum"]) == ("0.50", "1000.00")
    assert FACILITATOR.verify_calls == []


def test_unknown_donation_is_not_found(client: TestClient) -> None:
    assert client.post("/v1/donations/other", json={"amount_usd": "5.00"}).status_code == 404


# --------------------------------------------------------------------------- paying


def test_a_paid_donation_answers_with_a_signed_receipt(client: TestClient, engine: Engine) -> None:
    required = required_for(client, "5.00")
    payer = TestPayer()
    body = {"amount_usd": "5.00", "donor_name": "ResearchClaw-01", "donor_did": "did:web:a.b"}
    response = donate(client, body, payer.header(required))
    assert response.status_code == 201, response.text
    donation = response.json()
    settlement = decode(response.headers["PAYMENT-RESPONSE"])
    assert settlement["success"] is True and settlement["network"] == NETWORK
    assert settlement["payer"] == payer.address.lower()

    assert donation["status"] == "completed"
    assert donation["product_id"] == "inference_donation"
    assert donation["amount"] == {"currency": "USD", "amount": "5.00"}
    assert donation["payment"]["status"] == "settled"
    assert donation["payment"]["recipient"] == INFERENCE
    assert donation["payment"]["transaction"] == settlement["transaction"]
    assert donation["status_url"] == f"{BASE}/v1/donations/{donation['purchase_id']}"

    receipt = donation["receipt"]
    assert verify_proof(receipt, ISSUER.public_key_multibase)
    assert receipt["type"] == ["VerifiableCredential", "DebitMyDataDonationReceipt"]
    assert receipt["issuer"]["id"] == ISSUER.did
    subject = receipt["credentialSubject"]
    assert subject["id"] == "did:web:a.b"
    assert subject["donation"]["amount"] == {"currency": "USD", "amount": "5.00"}
    assert subject["donation"]["donorName"] == "ResearchClaw-01"
    assert subject["donation"]["termsVersion"] == "2026-10-25"
    assert subject["payment"]["payer"] == f"{NETWORK}:{payer.address.lower()}"
    assert subject["payment"]["transactions"] == [settlement["transaction"]]
    assert subject["payment"]["amount"] == "5.000000"

    # The funds reached the inference wallet, checked on chain, and nothing else moved.
    assert CHAIN.balances[INFERENCE.lower()] == FIVE
    assert MERCHANT.lower() not in CHAIN.balances
    pid = donation["payment"]["payment_intent_id"]
    assert (
        scalar(engine, "SELECT count(*) FROM credited_outputs WHERE payment_intent_id = :p", p=pid)
        == 1
    )

    # Readable later, and through the payment status.
    status = client.get(donation["status_url"].removeprefix(BASE))
    assert status.status_code == 200
    assert status.json()["receipt"] == receipt
    payment = client.get(f"/v1/payments/{pid}").json()
    assert payment["purchase_id"] == donation["purchase_id"]
    assert payment["registration_intent_id"] is None
    assert payment["next_action"] == "read_donation_status"
    assert actions(engine, donation["purchase_id"]) == [
        "purchase.created",
        "purchase.status_changed",
        "purchase.completed",
    ]


def test_without_a_donor_did_the_receipt_names_the_paying_wallet(client: TestClient) -> None:
    payer = TestPayer()
    response = donate(client, {"amount_usd": "0.50"}, payer.header(required_for(client, "0.50")))
    assert response.status_code == 201, response.text
    subject = response.json()["receipt"]["credentialSubject"]
    assert subject["id"] == f"did:pkh:{NETWORK}:{payer.address.lower()}"
    assert "donorName" not in subject["donation"]
    assert CHAIN.balances[INFERENCE.lower()] == 500_000


def test_the_same_payment_sent_again_is_the_same_donation(client: TestClient) -> None:
    header = TestPayer().header(required_for(client))
    first = donate(client, {"amount_usd": "5.00"}, header)
    assert first.status_code == 201, first.text
    again = donate(client, {"amount_usd": "5.00"}, header)
    assert again.status_code == 200, again.text
    assert again.json()["purchase_id"] == first.json()["purchase_id"]
    assert again.json()["receipt"] == first.json()["receipt"]
    assert len(FACILITATOR.settle_calls) == 1
    assert CHAIN.balances[INFERENCE.lower()] == FIVE

    # The same authorization cannot pay another amount either.
    other = donate(client, {"amount_usd": "6.00"}, header)
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "authorization_already_used"


def test_a_payment_for_another_amount_is_refused_before_the_facilitator(
    client: TestClient, engine: Engine
) -> None:
    purchases = scalar(engine, "SELECT count(*) FROM purchases")
    response = donate(client, {"amount_usd": "6.00"}, TestPayer().header(required_for(client)))
    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "payment_mismatch"
    assert "invalid_exact_evm_payload_authorization_value_mismatch" in error["reasons"]
    assert decode(response.headers["PAYMENT-REQUIRED"])["accepts"][0]["amount"] == "6000000"
    assert FACILITATOR.verify_calls == []
    assert scalar(engine, "SELECT count(*) FROM purchases") == purchases


def test_a_payment_to_another_wallet_is_refused(client: TestClient) -> None:
    header = TestPayer().header(required_for(client), to=MERCHANT)
    response = donate(client, {"amount_usd": "5.00"}, header)
    assert response.status_code == 402
    assert "invalid_exact_evm_payload_recipient_mismatch" in response.json()["error"]["reasons"]


@pytest.mark.parametrize("header", ["not base64!", "e30"])
def test_a_malformed_payment_is_a_bad_request(client: TestClient, header: str) -> None:
    response = donate(client, {"amount_usd": "5.00"}, header)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_payment"
    assert "PAYMENT-REQUIRED" in response.headers


def test_a_refused_payment_closes_its_donation_and_charges_nothing(
    client: TestClient, engine: Engine
) -> None:
    required = required_for(client)
    response = donate(client, {"amount_usd": "5.00"}, TestPayer(balance=1).header(required))
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payment_invalid"
    assert FACILITATOR.settle_calls == []
    assert (
        scalar(engine, "SELECT status FROM purchases ORDER BY created_at DESC LIMIT 1") == "failed"
    )
    assert INFERENCE.lower() not in CHAIN.balances

    response = donate(client, {"amount_usd": "5.00"}, TestPayer().header(required))
    assert response.status_code == 201, response.text


def test_a_failed_settlement_charges_nothing(client: TestClient, engine: Engine) -> None:
    FACILITATOR.settle_mode = "fail"
    response = donate(client, {"amount_usd": "5.00"}, TestPayer().header(required_for(client)))
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payment_failed"
    assert INFERENCE.lower() not in CHAIN.balances


def test_confirmations_still_coming_answer_202_then_the_receipt(
    client: TestClient, engine: Engine
) -> None:
    CHAIN.auto_confirm = 0
    header = TestPayer().header(required_for(client))
    response = donate(client, {"amount_usd": "5.00"}, header)
    assert response.status_code == 202, response.text
    assert response.headers["Retry-After"] == "10"
    assert decode(response.headers["PAYMENT-RESPONSE"])["success"] is True
    pending = response.json()
    assert pending["status"] == "payment_confirming"
    assert pending["receipt"] is None
    status_path = pending["status_url"].removeprefix(BASE)
    assert client.get(status_path).json()["status"] == "payment_confirming"

    CHAIN.mine(5)
    tick(client.app)  # the payment worker sees the confirmations
    done = client.get(status_path).json()
    assert done["status"] == "completed"
    assert verify_proof(done["receipt"], ISSUER.public_key_multibase)
    # Sending the payment again now returns the completed donation, never a second payment.
    again = donate(client, {"amount_usd": "5.00"}, header)
    assert again.status_code == 200
    assert again.json()["purchase_id"] == pending["purchase_id"]
    assert len(FACILITATOR.settle_calls) == 1


def test_a_reorg_after_the_receipt_raises_an_alert(client: TestClient, engine: Engine) -> None:
    response = donate(client, {"amount_usd": "5.00"}, TestPayer().header(required_for(client)))
    assert response.status_code == 201, response.text
    purchase_id = response.json()["purchase_id"]
    CHAIN.reorg()
    tick(client.app)
    assert "alert.completed_purchase_payment_reorged" in actions(engine, purchase_id)
    assert client.get(f"/v1/donations/{purchase_id}").json()["status"] == "completed"


def test_unknown_donation_status_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/donations/dmd_purchase_" + "0" * 26)
    assert response.status_code == 404
    assert client.get("/v1/donations/nope").status_code == 422


# --------------------------------------------------------------------------- availability


def test_without_a_receiving_wallet_donations_are_not_offered(
    registry: Path, engine: Engine
) -> None:
    environ = {k: v for k, v in ENVIRON.items() if k != "DMD_USDC_BASE_INFERENCE_ADDRESS"}
    with TestClient(make_app(registry, engine, environ=environ)) as closed:
        response = donate(closed, {"amount_usd": "5.00"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "donations_unavailable"
        assert response.headers["Retry-After"] == "3600"
        (offer,) = closed.get("/v1/discovery").json()["donations"]
        assert offer["accepting_payments"] is False
        options = closed.get("/v1/payment-options?service=inference_donation").json()
        assert options["supported_payment_options"] == []


def test_a_malformed_receiving_wallet_stops_the_gateway(registry: Path, engine: Engine) -> None:
    environ = {**ENVIRON, "DMD_USDC_BASE_INFERENCE_ADDRESS": "0x1234"}
    with pytest.raises(RegistryError, match="DMD_USDC_BASE_INFERENCE_ADDRESS"):
        make_app(registry, engine, environ=environ)


def test_no_payment_is_asked_when_receipts_cannot_be_signed(registry: Path, engine: Engine) -> None:
    with TestClient(make_app(registry, engine, issuer=None)) as unsigned:
        response = donate(unsigned, {"amount_usd": "5.00"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "receipts_unavailable"


def test_discovery_and_payment_options_list_the_donation(client: TestClient) -> None:
    (offer,) = client.get("/v1/discovery").json()["donations"]
    assert offer["id"] == "inference_donation"
    assert offer["path"] == DONATE
    assert offer["resource"] == f"{BASE}{DONATE}"
    assert offer["amount"] == {"currency": "USD", "minimum": "0.50", "maximum": "1000.00"}
    assert offer["accepting_payments"] is True
    assert offer["endpoints"]["donate"] == {
        "method": "POST",
        "url": f"{BASE}{DONATE}",
        "available": True,
    }
    options = client.get(offer["payment_options_url"].removeprefix(BASE)).json()
    (option,) = options["supported_payment_options"]
    assert option["id"] == "x402_usdc_base"
    assert (option["network"], option["asset"], option["payment_protocol"]) == (
        NETWORK,
        "USDC",
        "x402",
    )
    assert options["donation_amount"] == offer["amount"]
    assert options["commercial_fee"] is None


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
async def test_donating_through_mcp(registry: Path, engine: Engine, way: str) -> None:
    app = make_app(registry, engine)
    details = {"amount_usd": "2.50", "donor_name": "McpAgent"}
    arguments = {
        "product_id": "inference_donation",
        "preferred_payment_option": "x402_usdc_base",
        "details": details,
    }
    async with mcp_client(app) as mcp:
        catalog = (await mcp.call_tool("dmd_catalog", {})).structured_content
        donation = next(p for p in catalog["products"] if p["kind"] == "donation")
        assert donation["accepting_payments"] is True
        assert donation["payment_options"] == ["x402_usdc_base"]
        quote = await mcp.call_tool("dmd_get_purchase_quote", {"product_id": "inference_donation"})
        assert not quote.is_error, quote.content[0].text

        refused = await mcp.call_tool("dmd_purchase", arguments)
        assert refused.is_error
        required = json.loads(refused.content[0].text)  # the x402 PaymentRequired itself
        assert refused.structured_content == required
        assert required["accepts"][0]["amount"] == "2500000"
        assert required["accepts"][0]["payTo"] == INFERENCE

        payload = TestPayer().payload(required)
        if way == "argument":
            result = await mcp.call_tool("dmd_purchase", {**arguments, "x402_payment": payload})
        else:
            result = await mcp.call_tool("dmd_purchase", arguments, meta={"x402/payment": payload})
        wrong_option = await mcp.call_tool(
            "dmd_purchase", {**arguments, "preferred_payment_option": "dmd_pow_native"}
        )
    assert not result.is_error, result.content[0].text
    paid = result.structured_content
    assert paid["status"] == "completed"
    assert paid["receipt"]["credentialSubject"]["donation"]["donorName"] == "McpAgent"
    assert verify_proof(paid["receipt"], ISSUER.public_key_multibase)
    meta = result.meta["x402/payment-response"]
    assert meta["success"] is True and meta["transaction"] == paid["payment"]["transaction"]
    assert wrong_option.is_error
    assert "payment_option_unavailable" in wrong_option.content[0].text


# --------------------------------------------------------------------------- staging


def test_staging_test_payer_signs_donations_on_base_sepolia_only(
    registry: Path, engine: Engine, tmp_path: Path
) -> None:
    payer = TestPayer()
    environ = {**ENVIRON, "DMD_STAGING_BASE_PAYER_KEY": payer.account.key.hex()}
    tool = "/v1/staging/test-donation-payments"

    app = make_app(registry, engine, environ=environ, staging_test_tools=True)
    with TestClient(app) as mainnet:
        response = mainnet.post(tool, json={"amount_usd": "2.50"})
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
    with TestClient(app) as staging:
        assert staging.post(tool, json={"amount_usd": "0.10"}).status_code == 422
        assert staging.post(tool, json={"amount_usd": "1", "donation_id": "x_y"}).status_code == 404
        response = staging.post(tool, json={"amount_usd": "2.5"})
        assert response.status_code == 201, response.text
        signed = response.json()
        assert (signed["amount_usd"], signed["asset"]) == ("2.50", "USDC")
        assert (signed["network"], signed["recipient"]) == ("eip155:84532", INFERENCE)
        assert signed["payer"] == payer.address
        payload = decode(signed["payment_signature"])
        assert recover_payer(payload, payload["accepted"]) == payer.address
        assert DONATE in signed["next_step"]

        # The same body as the docs page form sends.
        paid = staging.post(
            DONATE,
            data={"amount_usd": "2.50", "donor_name": ""},
            headers={"PAYMENT-SIGNATURE": signed["payment_signature"]},
        )
    assert paid.status_code == 201, paid.text
    assert decode(paid.headers["PAYMENT-RESPONSE"])["network"] == "eip155:84532"
    assert CHAIN.balances[INFERENCE.lower()] == 2_500_000
