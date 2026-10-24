"""Staging test tools and the docs pages.

Without a database: the tools are absent unless enabled, production refuses them, the docs
pages carry the DebitMyData favicon and the step-by-step guide, throwaway keys work, and the
test payer only ever pays on regtest. With TEST_DATABASE_URL: the whole registration driven
only through the docs-page endpoints, with a scripted chain behind the DMD adapter.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alembic import command
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, select

from dmd_gateway.db.models import AuditEvent
from dmd_gateway.db.session import session_factory
from dmd_gateway.identity.crypto import parse_controller_key, verify_proof
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.identity.issuer import IssuerKey
from dmd_gateway.main import create_app
from dmd_gateway.services.settlement import run_pass
from dmd_gateway.services.staging_tools import (
    RegtestPayer,
    StagingPayment,
    StagingToolError,
    load_test_agent_key,
    new_test_agent_key,
    public_key_of,
)
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL, UNREACHABLE_DB, alembic_config
from .fakes import AGENT_CONTROLLER, SCRIPTED, fixed_price_factory
from .test_quotes import ADAPTERS, PRICE_SOURCE, ready_registry  # noqa: F401

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
KEYS = "/v1/staging/test-agent-keys"
PAYMENTS = "/v1/staging/test-payments"
SIGNATURES = "/v1/staging/test-signatures"
PAYMENT_ID = "dmd_payment_intent_" + "0" * 26
CHALLENGE_ID = "dmd_challenge_" + "0" * 26


def staging_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "database_url": TEST_DATABASE_URL or UNREACHABLE_DB,
        "database_connect_timeout_seconds": 1,
        "route_registry_path": REGISTRY_PATH,
        "staging_test_tools": True,
    }
    return Settings(**{**values, **overrides})


@pytest.fixture
def staging_client() -> Iterator[TestClient]:
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    app = create_app(staging_settings(), environ={}, engine=engine)
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- no database


def test_production_refuses_the_staging_tools() -> None:
    with pytest.raises(ValidationError, match="never be enabled in production"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+psycopg://gw:secret@db:5432/gw",
            public_base_url="https://api.example.com",
            staging_test_tools=True,
        )


def test_tools_are_absent_unless_enabled(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert not [path for path in schema["paths"] if path.startswith("/v1/staging")]
    assert "staging" not in {tag["name"] for tag in schema["tags"]}
    assert "Try the whole registration" not in schema["info"]["description"]
    assert client.post(KEYS).status_code == 404


def test_docs_pages_carry_the_dmd_favicon(client: TestClient) -> None:
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert 'href="/favicon.png"' in docs.text
    assert '"tryItOutEnabled": true' in docs.text
    assert '"deepLinking": true' in docs.text
    redoc = client.get("/redoc")
    assert redoc.status_code == 200
    assert 'href="/favicon.png"' in redoc.text
    png = client.get("/favicon.png")
    assert png.headers["content-type"] == "image/png"
    assert png.content.startswith(b"\x89PNG")
    ico = client.get("/favicon.ico")
    assert ico.headers["content-type"] == "image/x-icon"
    assert ico.content[:4] == b"\x00\x00\x01\x00"


def test_docs_page_guide_links_to_each_endpoint(staging_client: TestClient) -> None:
    schema = staging_client.get("/openapi.json").json()
    operations = {
        (operation["tags"][0], operation["operationId"]): path
        for path, methods in schema["paths"].items()
        for operation in methods.values()
    }
    assert {KEYS, PAYMENTS, SIGNATURES} <= set(schema["paths"])
    assert [tag["name"] for tag in schema["tags"]][:3] == ["discovery", "staging", "registration"]
    guide = schema["info"]["description"]
    links = re.findall(r"\]\(#/([a-z]+)/([A-Za-z0-9_]+)\)", guide)
    assert len(links) == 8
    linked = [operations[link] for link in links]  # every link names a real operation
    assert linked[0] == KEYS and linked[2] == PAYMENTS and linked[5] == SIGNATURES
    assert linked[7] == "/v1/dids/registrations"


def test_throwaway_keys_round_trip(staging_client: TestClient) -> None:
    response = staging_client.post(KEYS)
    assert response.status_code == 201
    body = response.json()
    assert body["private_key"].startswith("z3u2")
    assert (
        parse_controller_key(body["controller_public_key"]).canonical
        == (body["controller_public_key"])
    )
    key = load_test_agent_key(body["private_key"])
    assert public_key_of(key) == body["controller_public_key"]
    assert staging_client.post(KEYS).json()["private_key"] != body["private_key"]


@pytest.mark.parametrize(
    "text", ["", "not-multibase", "z" + "1" * 45, AGENT_CONTROLLER, AGENT_CONTROLLER[8:]]
)
def test_only_throwaway_private_keys_load(text: str) -> None:
    with pytest.raises(StagingToolError):
        load_test_agent_key(text)


def test_signing_refuses_a_foreign_private_key(staging_client: TestClient) -> None:
    response = staging_client.post(
        SIGNATURES, json={"challenge_id": CHALLENGE_ID, "private_key": "z" + "2" * 45}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_private_key"


class NodeScript:
    """A fake DMD node behind httpx.MockTransport, answering the test payer's calls."""

    def __init__(self, chain: str = "regtest", balances: list[str] | None = None) -> None:
        self.chain = chain
        self.balances = [Decimal(b) for b in (balances or ["100"])]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method, params = payload["method"], payload["params"]
        self.calls.append((method, params))
        assert request.url.path == "/wallet/payer-test"
        results = {
            "getblockchaininfo": {"chain": self.chain},
            "getnewaddress": "dmrt1qminer",
            "sendtoaddress": "ab" * 32,
            "generatetoaddress": ["00" * 32] * params.get("nblocks", 0),
        }
        if method == "getbalance":
            result: Any = str(self.balances.pop(0) if len(self.balances) > 1 else self.balances[0])
            return httpx.Response(200, content=f'{{"result": {result}, "error": null}}')
        return httpx.Response(200, json={"result": results[method], "error": None})

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def payer_for(script: NodeScript) -> RegtestPayer:
    return RegtestPayer(
        "http://127.0.0.1:58557", "gw", "pw", "payer-test", transport=httpx.MockTransport(script)
    )


def test_payer_pays_the_exact_amount_then_mines_the_confirmations() -> None:
    script = NodeScript()
    payment = payer_for(script).pay("dmrt1qquote", Decimal("25.00000000"), 6)
    assert payment == StagingPayment("ab" * 32, 6, topped_up=False)
    assert script.methods() == [
        "getblockchaininfo",
        "getnewaddress",
        "getbalance",
        "sendtoaddress",
        "generatetoaddress",
    ]
    assert script.calls[3][1] == {"address": "dmrt1qquote", "amount": "25.00000000"}
    assert script.calls[4][1] == {"nblocks": 6, "address": "dmrt1qminer"}


def test_payer_mines_test_coins_when_the_wallet_runs_low() -> None:
    script = NodeScript(balances=["3", "350"])
    assert payer_for(script).pay("dmrt1qquote", Decimal("25"), 6).topped_up
    assert script.calls[3] == ("generatetoaddress", {"nblocks": 260, "address": "dmrt1qminer"})
    with pytest.raises(StagingToolError, match="no spendable test coins"):
        payer_for(NodeScript(balances=["3"])).pay("dmrt1qquote", Decimal("25"), 6)


def test_payer_never_sends_outside_regtest() -> None:
    script = NodeScript(chain="main")
    with pytest.raises(StagingToolError, match="only run on a regtest chain"):
        payer_for(script).pay("dm1qsomeone", Decimal("25"), 6)
    assert script.methods() == ["getblockchaininfo"]


def test_payer_has_its_own_short_method_list() -> None:
    payer = payer_for(NodeScript())
    for method in ("dumpprivkey", "walletpassphrase", "stop", "invalidateblock"):
        with pytest.raises(StagingToolError, match="not available to the test payer"):
            payer.call(method)
    with pytest.raises(StagingToolError, match="private IP"):
        RegtestPayer("http://8.8.8.8:58557", "gw", "pw", "payer-test")


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


@pytest.fixture
def regtest_registry(ready_registry: Path, tmp_path: Path) -> Path:  # noqa: F811
    data = yaml.safe_load(ready_registry.read_text(encoding="utf-8"))
    dmd = data["rails"]["dmd_pow_native"]
    dmd["network"] = "dmd:regtest"
    dmd["asset"]["canonical_id"] = "dmd:regtest/native"
    path = tmp_path / "payment_routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class ScriptedPayer:
    """Pays on the scripted chain: the fake DMD adapter then sees the payment."""

    def __init__(self) -> None:
        self.paid: list[tuple[str, Decimal, int]] = []

    def pay(self, address: str, amount: Decimal, confirmations: int) -> StagingPayment:
        self.paid.append((address, amount, confirmations))
        return StagingPayment("cd" * 32, confirmations, topped_up=False)


def tick(app: FastAPI) -> None:
    state = app.state
    run_pass(session_factory(state.engine), state.quote_context.adapters, datetime.now(UTC))


@needs_db
def test_whole_registration_from_the_docs_page_endpoints(
    regtest_registry: Path, engine: Engine
) -> None:
    SCRIPTED.reset()
    issuer = IssuerKey(Ed25519PrivateKey.generate(), issuer_did("http://localhost:8000"))
    app = create_app(
        staging_settings(route_registry_path=regtest_registry),
        environ={"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"},
        engine=engine,
        adapters=ADAPTERS,
        price_sources={PRICE_SOURCE: fixed_price_factory(PRICE_SOURCE, "1.00")},
        issuer_key=issuer,
    )
    payer = ScriptedPayer()
    app.state.staging_payer = payer
    client = TestClient(app)

    # 1. A throwaway agent key, 2. a quote for it.
    key = client.post(KEYS).json()
    quote = client.post(
        "/v1/dids/registration-quotes",
        json={
            "display_name": "DocsPageAgent-01",
            "participant_type": "autonomous_agent",
            "controller_public_key": key["controller_public_key"],
            "preferred_payment_option": "dmd_pow_native",
        },
    )
    assert quote.status_code == 201, quote.text
    rid, pid = quote.json()["registration_intent_id"], quote.json()["payment_intent_id"]
    option = quote.json()["selected_payment_option"]

    # 3. Test payment: the exact quoted amount to the quoted address, once only.
    paid = client.post(PAYMENTS, json={"payment_intent_id": pid})
    assert paid.status_code == 201, paid.text
    assert paid.json()["amount"] == option["amount"] == "25.00000000"
    assert payer.paid == [(option["recipient"], Decimal("25.00000000"), 6)]
    again = client.post(PAYMENTS, json={"payment_intent_id": pid})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_test_paid")
    assert len(payer.paid) == 1
    with session_factory(engine)() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "staging.test_payment_sent")
        ).one()
    assert event.subject_id == pid
    assert event.data["transaction_reference"] == "cd" * 32

    # The chain sees it; the worker settles it (4.).
    SCRIPTED.pay(pid, int(option["amount_base_units"]), txid="cd" * 32)
    SCRIPTED.confirm(pid, 6)
    tick(app)
    assert client.get(f"/v1/payments/{pid}").json()["status"] == "settled"
    settled = client.post(PAYMENTS, json={"payment_intent_id": pid})
    assert settled.json()["error"]["code"] == "not_awaiting_payment"

    # 5. Challenge, 6. a signature with the wrong key, then the right one, 7. the proof.
    challenge = client.post(f"/v1/dids/registrations/{rid}/controller-challenges").json()
    wrong = client.post(
        SIGNATURES,
        json={
            "challenge_id": challenge["challenge_id"],
            "private_key": new_test_agent_key().private_key,
        },
    )
    assert wrong.json()["matches_controller"] is False
    signed = client.post(
        SIGNATURES,
        json={"challenge_id": challenge["challenge_id"], "private_key": key["private_key"]},
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["matches_controller"] is True
    proof = client.post(
        f"/v1/dids/registrations/{rid}/controller-proofs",
        json={"challenge_id": challenge["challenge_id"], "signature": signed.json()["signature"]},
    )
    assert proof.status_code == 201, proof.text
    assert proof.json()["status"] == "verified"
    used = client.post(
        SIGNATURES,
        json={"challenge_id": challenge["challenge_id"], "private_key": key["private_key"]},
    )
    assert used.json()["error"]["code"] == "challenge_closed"

    # 8. The DID.
    issued = client.post("/v1/dids/registrations", json={"registration_intent_id": rid})
    assert issued.status_code == 201, issued.text
    assert issued.json()["entitlements"] == ["dmd_did_holder"]
    assert verify_proof(issued.json()["credential"], issuer.public_key_multibase)


@needs_db
def test_only_unpaid_test_chain_quotes_can_be_test_paid(
    ready_registry: Path,  # noqa: F811
    engine: Engine,
) -> None:
    app = create_app(
        staging_settings(route_registry_path=ready_registry),
        environ={"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"},
        engine=engine,
        adapters=ADAPTERS,
        price_sources={PRICE_SOURCE: fixed_price_factory(PRICE_SOURCE, "1.00")},
    )
    payer = ScriptedPayer()
    app.state.staging_payer = payer
    client = TestClient(app)
    assert client.post(PAYMENTS, json={"payment_intent_id": PAYMENT_ID}).status_code == 404

    # The shipped registry's DMD rail is mainnet: never test-paid.
    quote = client.post(
        "/v1/dids/registration-quotes",
        json={
            "display_name": "MainnetAgent-01",
            "participant_type": "autonomous_agent",
            "controller_public_key": new_test_agent_key().controller_public_key,
            "preferred_payment_option": "dmd_pow_native",
        },
    )
    assert quote.status_code == 201, quote.text
    response = client.post(PAYMENTS, json={"payment_intent_id": quote.json()["payment_intent_id"]})
    assert (response.status_code, response.json()["error"]["code"]) == (409, "not_a_test_payment")
    assert payer.paid == []
