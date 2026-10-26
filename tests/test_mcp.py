"""The MCP endpoint, exercised with the official MCP client over Streamable HTTP.

Without a database: tool listing, discovery tools, error reporting and transport checks.
With TEST_DATABASE_URL: the whole registration through MCP tools only, from the offer to
the DID, with a scripted chain behind the DMD adapter.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from alembic import command
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import Engine, create_engine

from dmd_gateway.db.session import session_factory
from dmd_gateway.identity.crypto import verify_proof
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.identity.issuer import IssuerKey
from dmd_gateway.main import create_app
from dmd_gateway.services.settlement import run_pass
from dmd_gateway.settings import Settings

from .conftest import TEST_DATABASE_URL, UNREACHABLE_DB, alembic_config
from .fakes import AGENT_CONTROLLER, SCRIPTED, sign_challenge
from .test_quotes import _app_client, ready_registry  # noqa: F401

pytestmark = pytest.mark.anyio

BASE = "http://localhost:8000"
DIRECTIVE_TOOLS = {
    "dmd_discover",
    "dmd_get_did_registration_offer",
    "dmd_list_supported_payment_options",
    "dmd_create_did_payment_quote",
    "dmd_submit_payment_reference",
    "dmd_get_payment_status",
    "dmd_prove_wallet_control",
    "dmd_register_did",
    "dmd_catalog",
    "dmd_get_purchase_quote",
    "dmd_purchase",
    "dmd_marketplace_provider_requirements",
}
needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """A gateway with every rail off and no database (lifespan started by mcp_client)."""
    return create_app(settings, environ={}, engine=create_engine(UNREACHABLE_DB))


@asynccontextmanager
async def mcp_client(app: FastAPI) -> AsyncIterator[Client]:
    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)
        async with http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
            yield client


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def call_error(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    text = result.content[0].text
    return json.loads(text[text.index("{") :])


# --------------------------------------------------------------------------- no database


async def test_tools_cover_the_directive(app: FastAPI) -> None:
    async with mcp_client(app) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
    assert DIRECTIVE_TOOLS <= set(tools)
    assert tools["dmd_discover"].annotations.read_only_hint is True
    assert tools["dmd_register_did"].annotations.idempotent_hint is True
    schema = tools["dmd_create_did_payment_quote"].input_schema
    assert set(schema["required"]) == {
        "display_name",
        "participant_type",
        "preferred_payment_option",
    }
    assert {"controller_public_key", "controller_wallet_address"} <= set(schema["properties"])
    for tool in tools.values():
        assert tool.description, tool.name


async def test_discovery_tools(app: FastAPI) -> None:
    async with mcp_client(app) as mcp:
        discovery = await call(mcp, "dmd_discover")
        offer = await call(mcp, "dmd_get_did_registration_offer")
        catalog = await call(mcp, "dmd_catalog")
        marketplace = await call(mcp, "dmd_marketplace_provider_requirements")
    assert "DebitMyData Proof-of-Work blockchain" in discovery["about"]
    assert discovery["mcp_url"] == f"{BASE}/mcp"
    assert offer["offer"]["commercial_price"] == {"currency": "USD", "amount": "25.00"}
    assert offer["payment_options"]["supported_payment_options"] == []  # every rail is off
    product, donation = catalog["products"]
    assert product["product_id"] == "did_registration"
    assert product["kind"] == "fixed_price"
    assert product["payment_options"] == []
    assert donation["product_id"] == "inference_donation"
    assert donation["kind"] == "donation"
    assert donation["amount"] == {"currency": "USD", "minimum": "0.50", "maximum": "1000.00"}
    assert donation["accepting_payments"] is False
    assert donation["payment_options"] == []
    assert marketplace["status"] == "not_open"


async def test_errors_carry_the_rest_error_code(app: FastAPI) -> None:
    async with mcp_client(app) as mcp:
        unavailable = await call_error(
            mcp,
            "dmd_create_did_payment_quote",
            display_name="McpAgent",
            participant_type="ai_agent",
            controller_public_key=AGENT_CONTROLLER,
            preferred_payment_option="dmd_pow_native",
        )
        bad_key = await call_error(
            mcp,
            "dmd_create_did_payment_quote",
            display_name="McpAgent",
            participant_type="ai_agent",
            controller_public_key="ed25519:not-a-key",
            preferred_payment_option="dmd_pow_native",
        )
        unknown = await call_error(
            mcp, "dmd_purchase", product_id="nft", preferred_payment_option="x", details={}
        )
        half_proof = await call_error(
            mcp,
            "dmd_prove_wallet_control",
            registration_intent_id="dmd_did_intent_" + "0" * 26,
            challenge_id="dmd_challenge_" + "0" * 26,
        )
    assert unavailable["http_status"] == 409
    assert unavailable["error"]["code"] == "payment_option_unavailable"
    assert bad_key["http_status"] == 422
    assert unknown["error"]["code"] == "unknown_product"
    assert half_proof["error"]["code"] == "invalid_request"


def test_older_protocol_handshake_and_host_check(client: TestClient) -> None:
    headers = {"Accept": "application/json, text/event-stream", "Host": "localhost:8000"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "raw-test", "version": "1"},
        },
    }
    response = client.post("/mcp", json=initialize, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "debitmydata-agent-gateway"
    assert "dmd_create_did_payment_quote" in result["instructions"]

    # DNS-rebinding protection: an unexpected Host or Origin is refused.
    assert client.post(
        "/mcp", json=initialize, headers={**headers, "Host": "evil.example"}
    ).is_error
    evil_origin = {**headers, "Origin": "https://evil.example"}
    assert client.post("/mcp", json=initialize, headers=evil_origin).is_error


# --------------------------------------------------------------------------- full flow


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()
    command.downgrade(config, "base")


def tick(app: FastAPI) -> None:
    state = app.state
    run_pass(session_factory(state.engine), state.quote_context.adapters, datetime.now(UTC))


@needs_db
async def test_registration_through_mcp_only(
    ready_registry: Path,  # noqa: F811
    engine: Engine,
) -> None:
    SCRIPTED.reset()
    issuer = IssuerKey(Ed25519PrivateKey.generate(), issuer_did(BASE))
    app = _app_client(ready_registry, engine, issuer_key=issuer).app
    async with mcp_client(app) as mcp:
        offer = await call(mcp, "dmd_get_did_registration_offer")
        options = [o["id"] for o in offer["payment_options"]["supported_payment_options"]]
        assert "dmd_pow_native" in options

        quote = await call(
            mcp,
            "dmd_create_did_payment_quote",
            display_name="McpAgent-01",
            participant_type="ai_agent",
            controller_public_key=AGENT_CONTROLLER,
            preferred_payment_option="dmd_pow_native",
            idempotency_key="mcp-test-quote-0001",
        )
        rid, pid = quote["registration_intent_id"], quote["payment_intent_id"]
        assert quote["commercial_price"] == {"currency": "USD", "amount": "25.00"}
        units = int(quote["selected_payment_option"]["amount_base_units"])

        # The agent pays from its wallet and reports the transaction (a hint only).
        txid = "9f" * 32
        reference = await call(
            mcp, "dmd_submit_payment_reference", payment_intent_id=pid, transaction_reference=txid
        )
        assert reference["status"] == "recorded"
        SCRIPTED.pay(pid, units, txid=txid)
        tick(app)
        assert txid in SCRIPTED.expected[pid].known_transactions
        SCRIPTED.confirm(pid, 6)
        tick(app)
        payment = await call(mcp, "dmd_get_payment_status", payment_intent_id=pid)
        assert (payment["status"], payment["next_action"]) == ("settled", "prove_wallet_control")

        challenge = await call(mcp, "dmd_prove_wallet_control", registration_intent_id=rid)
        assert challenge["step"] == "sign_challenge"
        proof = await call(
            mcp,
            "dmd_prove_wallet_control",
            registration_intent_id=rid,
            challenge_id=challenge["challenge_id"],
            signature=sign_challenge(challenge["message"]),
        )
        assert (proof["status"], proof["next_action"]) == ("verified", "register_did")

        issued = await call(mcp, "dmd_register_did", registration_intent_id=rid)
        again = await call(mcp, "dmd_register_did", registration_intent_id=rid)
        status = await call(mcp, "dmd_get_registration_status", registration_intent_id=rid)

    assert issued["did"].startswith("did:web:localhost%3A8000:agents:")
    assert issued["entitlements"] == ["dmd_did_holder"]
    assert again["did"] == issued["did"]
    assert status["status"] == "issued"
    assert verify_proof(issued["credential"], issuer.public_key_multibase)


@needs_db
async def test_a_wallet_proves_control_through_mcp(
    ready_registry: Path,  # noqa: F811
    engine: Engine,
) -> None:
    owner = Account.create()
    app = _app_client(ready_registry, engine).app
    async with mcp_client(app) as mcp:
        quote = await call(
            mcp,
            "dmd_create_did_payment_quote",
            display_name="McpWallet-01",
            participant_type="ai_agent",
            controller_public_key=AGENT_CONTROLLER,
            controller_wallet_address=owner.address,
            controller_network="eip155:8453",
            preferred_payment_option="dmd_pow_native",
        )
        rid = quote["registration_intent_id"]
        challenge = await call(
            mcp, "dmd_prove_wallet_control", registration_intent_id=rid, controller_type="eip155"
        )
        assert challenge["controller"] == f"eip155:8453:{owner.address}"
        assert "personal_sign" in challenge["how_to_answer"]
        signed = owner.sign_message(encode_defunct(text=challenge["message"]))
        proof = await call(
            mcp,
            "dmd_prove_wallet_control",
            registration_intent_id=rid,
            challenge_id=challenge["challenge_id"],
            signature="0x" + bytes(signed.signature).hex(),
        )
        assert proof["status"] == "verified"
        status = await call(mcp, "dmd_get_registration_status", registration_intent_id=rid)
        assert status["controller"]["still_to_prove"] == ["ed25519"]
        # The next challenge, by default, is for the key still to prove.
        nxt = await call(mcp, "dmd_prove_wallet_control", registration_intent_id=rid)
        assert nxt["controller"] == AGENT_CONTROLLER
        assert "Ed25519" in nxt["how_to_answer"]
