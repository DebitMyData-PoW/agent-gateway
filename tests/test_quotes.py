"""POST /v1/dids/registration-quotes.

Validation and availability tests run everywhere. Tests that create quotes need the
disposable PostgreSQL database named by TEST_DATABASE_URL and are skipped without it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.identity.issuer import IssuerKey
from dmd_gateway.main import create_app
from dmd_gateway.services.staging_tools import new_test_agent_key
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL, UNREACHABLE_DB, alembic_config
from .fakes import (
    AGENT_CONTROLLER,
    AGENT_PUBLIC_HEX,
    FakeDmdAdapter,
    FakeX402Adapter,
    fixed_price_factory,
)

URL = "/v1/dids/registration-quotes"
MERCHANT = "0x1111111111111111111111111111111111111111"
USDC_ID = "eip155:8453/erc20:0x2222222222222222222222222222222222222222"
PRICE_SOURCE = "approved_dmd_usd_source"

ADAPTERS = {"DMDNativePaymentAdapter": FakeDmdAdapter, "X402PaymentAdapter": FakeX402Adapter}
ENABLED = {
    "DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true",
    "DMD_RAIL_X402_USDC_BASE_ENABLED": "true",
    "DMD_USDC_BASE_MERCHANT_ADDRESS": MERCHANT,
    "DMD_X402_FACILITATOR_URL": "https://facilitator.test",
    "DMD_BASE_RPC_URL": "https://rpc.test",
}

# The directive's example request.
EXAMPLE_REQUEST: dict[str, Any] = {
    "display_name": "ResearchClaw-01",
    "participant_type": "autonomous_agent",
    "controller_public_key": f"ed25519:{AGENT_PUBLIC_HEX}",
    # The directive's example also names a wallet (0x000...0, a placeholder); a wallet is a
    # controller that must prove control too, so wallet tests name a real one.
    "preferred_payment_option": "x402_usdc_base",
    "metadata": {"service_category": "research-agent"},
}

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def _request(**changes: Any) -> dict[str, Any]:
    body = {**EXAMPLE_REQUEST, **changes}
    return {k: v for k, v in body.items() if v is not None}


@pytest.fixture(scope="module")
def ready_registry(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The shipped registry with the open decisions filled in with test values."""
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    dmd = data["rails"]["dmd_pow_native"]
    dmd["confirmation_policy"]["required_confirmations"] = 6
    dmd["price_policy"] = {
        "source": PRICE_SOURCE,
        "max_price_age_seconds": 300,
        "max_deviation_bps": 200,
        "fallback": "refuse_quote",
    }
    usdc = data["rails"]["x402_usdc_base"]
    usdc["asset"]["canonical_id"] = USDC_ID
    path = tmp_path_factory.mktemp("registry") / "payment_routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _app_client(
    registry_path: Path,
    engine: Engine,
    *,
    environ: dict[str, str] | None = None,
    price: str = "0.2",
    issuer_key: IssuerKey | None = None,
    settings_overrides: dict[str, Any] | None = None,
    **price_kwargs: object,
) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL or UNREACHABLE_DB,
        route_registry_path=registry_path,
        **(settings_overrides or {}),
    )
    app = create_app(
        settings,
        environ=ENABLED if environ is None else environ,
        engine=engine,
        adapters=ADAPTERS,
        price_sources={PRICE_SOURCE: fixed_price_factory(PRICE_SOURCE, price, **price_kwargs)},
        issuer_key=issuer_key,
    )
    return TestClient(app)


# --------------------------------------------------------------------------- no database


def test_no_enabled_rail_means_no_quote(client: TestClient) -> None:
    response = client.post(URL, json=EXAMPLE_REQUEST)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "payment_option_unavailable"
    assert error["enabled_payment_options"] == []


def test_unknown_payment_option(client: TestClient) -> None:
    response = client.post(URL, json=_request(preferred_payment_option="any_crypto"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_payment_option"


@pytest.mark.parametrize(
    "changes",
    [
        {"controller_public_key": None, "controller_wallet_address": None},
        {"participant_type": "robot_overlord"},
        {"display_name": ""},
        {"display_name": "x" * 101},
        {"display_name": "bad\nname"},
        {"controller_public_key": "not a key"},
        {"controller_public_key": "secp256k1:" + "02" + "11" * 32},
        {"controller_public_key": "ed25519:01" + "00" * 31},  # weak key (the identity point)
        {"controller_public_key": "ed25519:" + "zz" * 32},
        {"controller_wallet_address": "short"},
        {"controller_wallet_address": "0x" + "00" * 20},  # the zero address: no one's
        # A mistyped capital letter breaks the checksum (EIP-55).
        {"controller_wallet_address": "0x52908400098527886E0F7030069857D2E4169eE7"},
        {"controller_network": "eip155:8453"},  # a network without a wallet
        {"metadata": {f"k{i}": i for i in range(21)}},
        {"metadata": {"nested": {"not": "allowed"}}},
        {"metadata": {"long": "x" * 300}},
        {"unexpected_field": True},
    ],
)
def test_invalid_requests_are_rejected(client: TestClient, changes: dict[str, Any]) -> None:
    body = {**EXAMPLE_REQUEST, **changes}
    body = {k: v for k, v in body.items() if v is not None}
    assert client.post(URL, json=body).status_code == 422


def test_bad_idempotency_key_is_rejected(client: TestClient) -> None:
    response = client.post(URL, json=EXAMPLE_REQUEST, headers={"Idempotency-Key": "short"})
    assert response.status_code == 422


def test_killed_rail_gives_no_quote(ready_registry: Path) -> None:
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    environ = {**ENABLED, "DMD_RAIL_X402_USDC_BASE_KILL": "true"}
    with _app_client(ready_registry, engine, environ=environ) as client:
        response = client.post(URL, json=EXAMPLE_REQUEST)
    assert response.status_code == 409
    assert response.json()["error"]["enabled_payment_options"] == ["dmd_pow_native"]


def test_database_outage_is_reported_without_details(ready_registry: Path) -> None:
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    with _app_client(ready_registry, engine) as client:
        response = client.post(URL, json=EXAMPLE_REQUEST)
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Temporarily unavailable; try again later.",
        }
    }


# --------------------------------------------------------------------------- with database


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()
    command.downgrade(config, "base")


@pytest.fixture
def db_client(ready_registry: Path, engine: Engine) -> Iterator[TestClient]:
    with _app_client(ready_registry, engine) as client:
        yield client


def _count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


@needs_db
def test_usdc_quote_matches_the_directive(db_client: TestClient, engine: Engine) -> None:
    before = datetime.now(UTC)
    response = db_client.post(URL, json=EXAMPLE_REQUEST)
    assert response.status_code == 201, response.text
    quote = response.json()

    assert quote["registration_intent_id"].startswith("dmd_did_intent_")
    assert quote["payment_intent_id"].startswith("dmd_payment_intent_")
    assert quote["service"] == "DebitMyData Agentic DID Registration"
    assert quote["commercial_price"] == {"currency": "USD", "amount": "25.00"}
    option = quote["selected_payment_option"]
    assert option["id"] == "x402_usdc_base"
    assert option["asset"] == "USDC"
    assert option["network"] == "eip155:8453"
    assert option["payment_protocol"] == "x402"
    assert option["scheme"] == "exact"
    assert option["amount"] == "25.000000"
    assert option["amount_base_units"] == "25000000"
    assert option["recipient"] == MERCHANT
    assert option["token_contract"] == "0x2222222222222222222222222222222222222222"
    assert option["payment_reference"] == quote["payment_intent_id"]
    assert option["required_confirmations"] == 5
    assert option["transfer_method"] == "eip3009"
    assert quote["price_quote"] is None  # USD-denominated: no conversion
    assert quote["payment_status"] == "awaiting_payment"
    # x402: the controller is proven first; the payment answers the 402 of the registration.
    assert quote["next_action"] == "prove_wallet_control"
    assert quote["controller_proof_required"] is True
    assert quote["terms_version"] == "2026-10-25"
    # Links to the policy pages this server serves.
    assert quote["policy_urls"]["refund_policy"] == "http://localhost:8000/policies/refunds"

    expires = datetime.fromisoformat(quote["expires_at"])
    assert timedelta(seconds=890) < expires - before < timedelta(seconds=910)


@needs_db
def test_dmd_quote_uses_the_approved_price(db_client: TestClient, engine: Engine) -> None:
    response = db_client.post(URL, json=_request(preferred_payment_option="dmd_pow_native"))
    assert response.status_code == 201, response.text
    quote = response.json()
    option = quote["selected_payment_option"]
    assert option["asset"] == "DMD"
    assert option["network"] == "dmd:mainnet"
    assert option["payment_protocol"] == "dmd_native"
    assert option["amount"] == "125.00000000"  # 25.00 USD at 0.20 USD per DMD
    assert option["amount_base_units"] == "12500000000"
    assert option["required_confirmations"] == 6
    assert option["recipient"].startswith("dm1qtest")
    assert option["token_contract"] is None
    assert quote["next_action"] == "send_payment"
    price = quote["price_quote"]
    assert price["source"] == PRICE_SOURCE
    assert price["usd_per_unit"] == "0.2"
    assert price["expires_at"] == quote["expires_at"]


@needs_db
def test_amount_is_rounded_up(ready_registry: Path, engine: Engine) -> None:
    with _app_client(ready_registry, engine, price="3") as client:
        response = client.post(URL, json=_request(preferred_payment_option="dmd_pow_native"))
    assert response.status_code == 201, response.text
    # 25 / 3 = 8.333333333... -> never less than the commercial price
    assert response.json()["selected_payment_option"]["amount"] == "8.33333334"


@needs_db
def test_quote_is_stored_with_events_and_audit(db_client: TestClient, engine: Engine) -> None:
    quote = db_client.post(URL, json=_request(preferred_payment_option="dmd_pow_native")).json()
    registration_id = quote["registration_intent_id"]
    payment_id = quote["payment_intent_id"]
    with engine.connect() as conn:
        registration = conn.execute(
            text("SELECT * FROM registration_intents WHERE id = :id"), {"id": registration_id}
        ).one()
        payment = conn.execute(
            text("SELECT * FROM payment_intents WHERE id = :id"), {"id": payment_id}
        ).one()
    # Stored in its canonical (Multikey) form whatever encoding the agent used.
    assert registration.controller_public_key == AGENT_CONTROLLER
    assert registration.controller_wallet_address is None
    assert registration.agent_metadata == {"service_category": "research-agent"}
    assert registration.status == "awaiting_payment"
    assert payment.status == "awaiting_payment"
    assert int(payment.amount_base_units) == 12_500_000_000
    assert payment.price_source == PRICE_SOURCE
    assert payment.price_observed_at is not None
    assert payment.recipient == quote["selected_payment_option"]["recipient"]
    assert payment.recipient_kind == "per_intent_address"
    assert (
        _count(
            engine,
            "SELECT count(*) FROM payment_events WHERE payment_intent_id = :id"
            " AND to_status = 'awaiting_payment'",
            id=payment_id,
        )
        == 1
    )
    assert (
        _count(
            engine,
            "SELECT count(*) FROM audit_events WHERE subject_id = :id"
            " AND action = 'registration_quote.created'",
            id=registration_id,
        )
        == 1
    )


@needs_db
def test_retry_with_same_key_returns_the_same_quote(db_client: TestClient, engine: Engine) -> None:
    headers = {"Idempotency-Key": "8f14e45f-ceea-467a-9a3b-3c5a1d2e7b90"}
    first = db_client.post(URL, json=EXAMPLE_REQUEST, headers=headers)
    second = db_client.post(URL, json=EXAMPLE_REQUEST, headers=headers)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert (
        _count(
            engine,
            "SELECT count(*) FROM payment_intents WHERE registration_intent_id = :id",
            id=first.json()["registration_intent_id"],
        )
        == 1
    )


@needs_db
def test_reusing_a_key_for_a_different_request_is_rejected(db_client: TestClient) -> None:
    headers = {"Idempotency-Key": "3c2b1a00-aaaa-4bbb-8ccc-123456789abc"}
    assert db_client.post(URL, json=EXAMPLE_REQUEST, headers=headers).status_code == 201
    other = _request(display_name="SomeoneElse")
    response = db_client.post(URL, json=other, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"


@needs_db
def test_stale_price_refuses_the_quote_and_stores_nothing(
    ready_registry: Path, engine: Engine
) -> None:
    before = _count(engine, "SELECT count(*) FROM registration_intents")
    with _app_client(ready_registry, engine, age=timedelta(minutes=30)) as client:
        response = client.post(URL, json=_request(preferred_payment_option="dmd_pow_native"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "price_unavailable"
    assert response.headers["Retry-After"] == "60"
    assert _count(engine, "SELECT count(*) FROM registration_intents") == before


@needs_db
def test_every_quote_gets_its_own_dmd_address(db_client: TestClient) -> None:
    body = _request(preferred_payment_option="dmd_pow_native")
    addresses = {
        db_client.post(URL, json=body).json()["selected_payment_option"]["recipient"]
        for _ in range(5)
    }
    assert len(addresses) == 5


@needs_db
def test_open_quotes_are_limited_per_controller_key(ready_registry: Path, engine: Engine) -> None:
    limit = {"max_open_quotes_per_controller": 2}
    key = new_test_agent_key().controller_public_key
    with _app_client(ready_registry, engine, settings_overrides=limit) as client:
        first, second = (client.post(URL, json=_request(controller_public_key=key)) for _ in "12")
        assert first.status_code == second.status_code == 201
        refused = client.post(URL, json=_request(controller_public_key=key))
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "too_many_open_quotes"
        assert 0 < int(refused.headers["Retry-After"]) <= 900
        # Other keys are not affected, and a quote that has expired no longer counts.
        other = new_test_agent_key().controller_public_key
        assert client.post(URL, json=_request(controller_public_key=other)).status_code == 201
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE registration_intents SET expires_at = now() - interval '1 second' "
                    "WHERE id = :id"
                ),
                {"id": first.json()["registration_intent_id"]},
            )
        assert client.post(URL, json=_request(controller_public_key=key)).status_code == 201
