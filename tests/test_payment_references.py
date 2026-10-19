"""POST /v1/payments/{id}/references: payer-submitted transaction references (hints only)."""

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

TXID = "AB" * 32


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
def client(ready_registry: Path, engine: Engine) -> Iterator[TestClient]:  # noqa: F811
    SCRIPTED.reset()
    with _app_client(ready_registry, engine) as test_client:
        yield test_client


def quote(client: TestClient) -> dict[str, Any]:
    body = {**EXAMPLE_REQUEST, "preferred_payment_option": "dmd_pow_native"}
    response = client.post(URL, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def submit(client: TestClient, payment_id: str, reference: str):
    return client.post(
        f"/v1/payments/{payment_id}/references", json={"transaction_reference": reference}
    )


def test_reference_is_recorded_once_and_given_to_the_adapter(client: TestClient) -> None:
    pid = quote(client)["payment_intent_id"]
    first = submit(client, pid, TXID)
    assert first.status_code == 202
    assert first.json()["transaction_reference"] == TXID.lower()
    assert first.json()["status"] == "recorded"
    again = submit(client, pid, TXID.lower())
    assert (again.status_code, again.json()["status"]) == (200, "already_recorded")

    state = client.app.state
    run_pass(session_factory(state.engine), state.quote_context.adapters, datetime.now(UTC))
    assert SCRIPTED.expected[pid].known_transactions == (TXID.lower(),)
    # A hint alone credits nothing: no output was observed.
    assert client.get(f"/v1/payments/{pid}").json()["status"] == "awaiting_payment"


@pytest.mark.parametrize("reference", ["not-a-txid-at-all", "zz" * 32, "ab" * 31, "0x" + "ab" * 32])
def test_dmd_references_must_be_transaction_ids(client: TestClient, reference: str) -> None:
    pid = quote(client)["payment_intent_id"]
    response = submit(client, pid, reference)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_reference"


def test_references_are_limited(client: TestClient) -> None:
    pid = quote(client)["payment_intent_id"]
    for i in range(5):
        assert submit(client, pid, f"{i:064x}").status_code == 202
    response = submit(client, pid, "f" * 64)
    assert response.status_code == 429


def test_closed_and_unknown_payments(client: TestClient) -> None:
    q = quote(client)
    state = client.app.state
    later = datetime.fromisoformat(q["expires_at"]) + timedelta(minutes=1)
    run_pass(session_factory(state.engine), state.quote_context.adapters, later)
    assert client.get(f"/v1/payments/{q['payment_intent_id']}").json()["status"] == "expired"
    closed = submit(client, q["payment_intent_id"], TXID)
    assert closed.status_code == 409
    assert closed.json()["error"]["code"] == "payment_not_open"
    assert submit(client, "dmd_payment_intent_" + "0" * 26, TXID).status_code == 404


def test_references_cannot_be_altered(client: TestClient, engine: Engine) -> None:
    pid = quote(client)["payment_intent_id"]
    submit(client, pid, TXID)
    with pytest.raises(Exception, match="append-only"), engine.begin() as conn:
        conn.execute(text("DELETE FROM payment_references"))
