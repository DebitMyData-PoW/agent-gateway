"""Payment worker against PostgreSQL, with a scripted chain behind the DMD adapter."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.db.session import session_factory
from dmd_gateway.services.settlement import run_pass

from .conftest import TEST_DATABASE_URL, alembic_config
from .fakes import SCRIPTED
from .test_quotes import EXAMPLE_REQUEST, URL, _app_client, ready_registry  # noqa: F401

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

DUE = 12_500_000_000  # 125 DMD at the 0.20 USD test price


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
    with _app_client(ready_registry, engine) as test_client:
        yield test_client


def quote(client: TestClient) -> dict[str, Any]:
    response = client.post(
        URL, json={**EXAMPLE_REQUEST, "preferred_payment_option": "dmd_pow_native"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["selected_payment_option"]["amount_base_units"] == str(DUE)
    return body


def tick(client: TestClient, now: datetime | None = None):
    state = client.app.state
    factory = session_factory(state.engine)
    return run_pass(factory, state.quote_context.adapters, now or datetime.now(UTC))


def status(client: TestClient, payment_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/payments/{payment_id}")
    assert response.status_code == 200, response.text
    return response.json()


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def test_full_payment_lifecycle(client: TestClient, engine: Engine) -> None:
    q = quote(client)
    pid, rid = q["payment_intent_id"], q["registration_intent_id"]
    assert status(client, pid)["status"] == "awaiting_payment"

    SCRIPTED.pay(pid, DUE, txid="t1")
    tick(client)
    s = status(client, pid)
    assert s["status"] == "confirming"
    assert s["amount_received"] == "125.00000000"
    assert s["confirmations"] == 0
    assert s["transactions"] == ["t1"]
    assert s["next_action"] == "wait_for_confirmations"

    SCRIPTED.confirm(pid, 1)
    tick(client)
    assert status(client, pid)["status"] == "verified"

    SCRIPTED.confirm(pid, 6)
    tick(client)
    s = status(client, pid)
    assert s["status"] == "settled"
    assert s["next_action"] == "prove_wallet_control"
    assert s["refund_due"] is None

    assert (
        scalar(engine, "SELECT count(*) FROM credited_outputs WHERE payment_intent_id = :p", p=pid)
        == 1
    )
    assert scalar(engine, "SELECT status FROM registration_intents WHERE id = :r", r=rid) == "paid"
    assert (
        scalar(
            engine,
            "SELECT count(*) FROM audit_events"
            " WHERE subject_id = :p AND action = 'payment.settled'",
            p=pid,
        )
        == 1
    )
    transitions = [
        tuple(row)
        for row in engine.connect().execute(
            text(
                "SELECT from_status, to_status FROM payment_events WHERE payment_intent_id = :p"
                " AND from_status IS DISTINCT FROM to_status ORDER BY id"
            ),
            {"p": pid},
        )
    ]
    assert transitions == [
        (None, "awaiting_payment"),
        ("awaiting_payment", "detected"),
        ("detected", "confirming"),
        ("confirming", "verified"),
        ("verified", "settled"),
    ]


def test_nothing_is_recorded_when_nothing_changed(client: TestClient, engine: Engine) -> None:
    pid = quote(client)["payment_intent_id"]
    count = "SELECT count(*) FROM payment_events WHERE payment_intent_id = :p"
    before = scalar(engine, count, p=pid)
    tick(client)
    tick(client)
    assert scalar(engine, count, p=pid) == before  # no payment, no new events


def test_node_outage_changes_nothing(client: TestClient) -> None:
    pid = quote(client)["payment_intent_id"]
    SCRIPTED.unavailable = True
    result = tick(client, datetime.now(UTC) + timedelta(hours=1))  # even past expiry
    assert result.unavailable >= 1
    assert status(client, pid)["status"] == "awaiting_payment"


def test_unpaid_quote_expires(client: TestClient, engine: Engine) -> None:
    q = quote(client)
    tick(client, datetime.now(UTC) + timedelta(minutes=16))
    assert status(client, q["payment_intent_id"])["status"] == "expired"
    assert (
        scalar(
            engine,
            "SELECT status FROM registration_intents WHERE id = :r",
            r=q["registration_intent_id"],
        )
        == "expired"
    )


def test_underpayment_is_not_credited_and_refund_is_recorded(
    client: TestClient, engine: Engine
) -> None:
    pid = quote(client)["payment_intent_id"]
    SCRIPTED.pay(pid, DUE - 1, txid="u1")
    tick(client)
    assert status(client, pid)["status"] == "detected"  # may still top up

    tick(client, datetime.now(UTC) + timedelta(minutes=16))
    s = status(client, pid)
    assert s["status"] == "underpaid"
    assert s["refund_due"] == "124.99999999"
    assert s["next_action"] == "contact_support_for_refund"
    assert (
        scalar(engine, "SELECT count(*) FROM credited_outputs WHERE payment_intent_id = :p", p=pid)
        == 0
    )
    assert (
        scalar(
            engine,
            "SELECT count(*) FROM audit_events"
            " WHERE subject_id = :p AND action = 'payment.refund_due'",
            p=pid,
        )
        == 1
    )


def test_overpayment_settles_and_records_the_excess(client: TestClient) -> None:
    pid = quote(client)["payment_intent_id"]
    SCRIPTED.pay(pid, DUE + 50_000_000, txid="o1", confirmations=6)
    tick(client)
    s = status(client, pid)
    assert s["status"] == "settled"
    assert s["refund_due"] == "0.50000000"


def test_reorg_reopens_a_settled_payment(client: TestClient, engine: Engine) -> None:
    q = quote(client)
    pid, rid = q["payment_intent_id"], q["registration_intent_id"]
    SCRIPTED.pay(pid, DUE, txid="r1", confirmations=6)
    tick(client)
    assert status(client, pid)["status"] == "settled"

    SCRIPTED.confirm(pid, 0)  # the block was disconnected; the tx is back in the mempool
    tick(client)
    assert status(client, pid)["status"] == "confirming"
    assert (
        scalar(engine, "SELECT status FROM registration_intents WHERE id = :r", r=rid)
        == "awaiting_payment"
    )

    SCRIPTED.confirm(pid, 6)  # mined again
    tick(client)
    assert status(client, pid)["status"] == "settled"
    # Credited once, even though it settled twice.
    assert (
        scalar(engine, "SELECT count(*) FROM credited_outputs WHERE payment_intent_id = :p", p=pid)
        == 1
    )


def test_double_spend_fails_the_payment(client: TestClient) -> None:
    pid = quote(client)["payment_intent_id"]
    SCRIPTED.pay(pid, DUE, txid="d1")
    tick(client)
    SCRIPTED.double_spend(pid)
    tick(client)
    assert status(client, pid)["status"] == "failed"


def test_late_payment_is_flagged_for_refund(client: TestClient, engine: Engine) -> None:
    pid = quote(client)["payment_intent_id"]
    later = datetime.now(UTC) + timedelta(minutes=16)
    tick(client, later)
    assert status(client, pid)["status"] == "expired"

    SCRIPTED.pay(pid, DUE, txid="l1", first_seen_at=later)
    tick(client, later + timedelta(minutes=1))
    s = status(client, pid)
    assert s["status"] == "expired"
    assert s["refund_due"] == "125.00000000"


def test_unknown_payment_is_404(client: TestClient) -> None:
    assert (
        client.get("/v1/payments/dmd_payment_intent_01M514JSYGREHRZYASK27W0YYF").status_code == 404
    )
    assert client.get("/v1/payments/not-an-id").status_code == 422
