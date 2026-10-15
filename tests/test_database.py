"""Schema tests against a real PostgreSQL database.

Skipped unless TEST_DATABASE_URL points at an empty, disposable database, e.g.
    TEST_DATABASE_URL=postgresql+psycopg://gateway:gateway-dev-only@localhost:5432/agent_gateway_test
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from .conftest import REPO_ROOT, TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.cmd_opts = type("Opts", (), {"x": [f"url={TEST_DATABASE_URL}"]})()
    return config


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()
    command.downgrade(config, "base")


def test_models_match_migrations(engine: Engine) -> None:
    command.check(_alembic_config())


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET action = 'tampered'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_audit_log_is_append_only(engine: Engine, statement: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_events (actor_type, action, subject_type, subject_id) "
                "VALUES ('system', 'test.event', 'test', 'subject-1')"
            )
        )
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text(statement))


def _insert_registration(conn, registration_id: str, **controller: str | None) -> None:
    controller = controller or {"public_key": "ed25519:TEST", "wallet_address": "0xTEST"}
    conn.execute(
        text(
            "INSERT INTO registration_intents (id, service_id, display_name, participant_type,"
            " controller_public_key, controller_wallet_address, commercial_price_currency,"
            " commercial_price_amount, terms_version, status, expires_at) VALUES"
            " (:id, 'did_registration', 'ResearchClaw-01', 'autonomous_agent', :pk, :wallet,"
            " 'USD', 25.00, '2026-09-18', 'awaiting_payment', :exp)"
        ),
        {
            "id": registration_id,
            "pk": controller.get("public_key"),
            "wallet": controller.get("wallet_address"),
            "exp": datetime.now(UTC) + timedelta(minutes=15),
        },
    )


def _insert_payment(
    conn, payment_id: str, registration_id: str, address: str, *, price_inputs: bool = True
) -> None:
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO payment_intents (id, registration_intent_id, rail_id, adapter,"
            " asset_symbol, asset_canonical_id, asset_decimals, network, amount_base_units,"
            " commercial_price_currency, commercial_price_amount, price_source,"
            " price_usd_per_unit, price_observed_at, recipient, recipient_kind,"
            " quote_expires_at, status) VALUES"
            " (:id, :reg, 'dmd_pow_native', 'DMDNativePaymentAdapter', 'DMD',"
            " 'dmd:mainnet/native', 8, 'dmd:mainnet', 12550000000, 'USD', 25.00, :source,"
            " :price, :observed, :addr, 'per_intent_address', :exp, 'awaiting_payment')"
        ),
        {
            "id": payment_id,
            "reg": registration_id,
            "source": "approved_dmd_usd_source",
            "price": "0.19920318725" if price_inputs else None,
            "observed": now if price_inputs else None,
            "addr": address,
            "exp": now + timedelta(minutes=15),
        },
    )


def _insert_intents(conn, suffix: str) -> str:
    registration_id = f"dmd_did_intent_{suffix}"
    payment_id = f"dmd_payment_intent_{suffix}"
    _insert_registration(conn, registration_id)
    _insert_payment(conn, payment_id, registration_id, f"dm1q{suffix}")
    return payment_id


def test_payment_output_cannot_be_credited_twice(engine: Engine) -> None:
    with engine.begin() as conn:
        first = _insert_intents(conn, "A1")
        second = _insert_intents(conn, "A2")
        conn.execute(
            text(
                "INSERT INTO credited_outputs (network, transaction_reference, output_index,"
                " payment_intent_id, amount_base_units) VALUES"
                " ('dmd:mainnet', 'txid-1', 0, :pid, 12550000000)"
            ),
            {"pid": first},
        )
    with pytest.raises(IntegrityError, match="pk_credited_outputs"), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credited_outputs (network, transaction_reference, output_index,"
                " payment_intent_id, amount_base_units) VALUES"
                " ('dmd:mainnet', 'txid-1', 0, :pid, 12550000000)"
            ),
            {"pid": second},
        )


def test_per_intent_address_cannot_be_reused(engine: Engine) -> None:
    with engine.begin() as conn:
        _insert_intents(conn, "B1")
    with (
        pytest.raises(IntegrityError, match="uq_payment_intents_per_intent_recipient"),
        engine.begin() as conn,
    ):
        _insert_payment(conn, "dmd_payment_intent_B2", "dmd_did_intent_B1", "dm1qB1")


def test_registration_needs_a_controller(engine: Engine) -> None:
    with (
        pytest.raises(IntegrityError, match="controller_present"),
        engine.begin() as conn,
    ):
        _insert_registration(conn, "dmd_did_intent_D1", public_key=None, wallet_address=None)


def test_wallet_address_alone_is_a_valid_controller(engine: Engine) -> None:
    with engine.begin() as conn:
        _insert_registration(conn, "dmd_did_intent_D2", wallet_address="0xONLY")


def test_price_inputs_are_all_or_nothing(engine: Engine) -> None:
    with engine.begin() as conn:
        _insert_registration(conn, "dmd_did_intent_E1")
    with (
        pytest.raises(IntegrityError, match="price_inputs_complete"),
        engine.begin() as conn,
    ):
        _insert_payment(
            conn, "dmd_payment_intent_E1", "dmd_did_intent_E1", "dm1qE1", price_inputs=False
        )


def test_unknown_payment_status_is_rejected(engine: Engine) -> None:
    with engine.begin() as conn:
        payment_id = _insert_intents(conn, "C1")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("UPDATE payment_intents SET status = 'paid_probably' WHERE id = :id"),
            {"id": payment_id},
        )
