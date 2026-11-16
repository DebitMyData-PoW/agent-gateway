"""PayPal payments through PayPal Checkout (Orders v2): the PayPal client, the adapter, the
route registry and, with a database, a whole PayPal registration (quote, approval, capture,
refund).

PayPal itself is replaced by tests/paypal_fakes.py: nothing here reaches the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.adapters import paypal_checkout
from dmd_gateway.adapters.base import (
    AdapterUnavailable,
    ExpectedPayment,
    PaymentDestination,
    QuotedPayment,
)
from dmd_gateway.adapters.paypal_api import PayPalClient, PayPalUnavailable
from dmd_gateway.adapters.paypal_checkout import (
    PayPalCheckoutAdapter,
    return_link,
    usd_cents,
    usd_text,
)
from dmd_gateway.db.session import session_factory
from dmd_gateway.domain.routes import RailState, RegistryError, load_registry
from dmd_gateway.main import create_app
from dmd_gateway.services.settlement import run_pass
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, REPO_ROOT, TEST_DATABASE_URL, alembic_config
from .fakes import AGENT_PUBLIC_HEX
from .paypal_fakes import CLIENT_ID, PAYPAL, SECRET, TOKEN, FakePayPal, FakePayPalAdapter

REGTEST_PATH = REPO_ROOT / "config" / "payment_routes.regtest.yaml"
PAYPAL_ENV = {
    "DMD_RAIL_PAYPAL_ENABLED": "true",
    "DMD_PAYPAL_CLIENT_ID": CLIENT_ID,
    "DMD_PAYPAL_CLIENT_SECRET": SECRET,
}
PAGE = "https://id.example.test/"
PAYMENT = "dmd_payment_intent_A"


def _rail() -> Any:
    registry = load_registry(
        REGTEST_PATH, environ=PAYPAL_ENV, available_adapters={"PayPalCheckoutAdapter"}
    )
    return registry.rails["paypal"]


def _adapter(paypal: FakePayPal) -> PayPalCheckoutAdapter:
    client = PayPalClient(CLIENT_ID, SECRET, live=False, transport=paypal.transport)
    return PayPalCheckoutAdapter("paypal", _rail(), PAYPAL_ENV, client=client)


def _quote(payment_id: str = PAYMENT, expires_in: int = 1860) -> QuotedPayment:
    return QuotedPayment(
        payment_intent_id=payment_id,
        registration_intent_id="dmd_did_intent_A",
        amount_base_units=2500,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        product_description="DID-LLM “Test”, registration dmd_did_intent_A",
        return_url=PAGE,
    )


def _expected(
    destination: PaymentDestination, payment_id: str = PAYMENT, ends_in: int = 1800
) -> ExpectedPayment:
    return ExpectedPayment(
        payment_intent_id=payment_id,
        network="paypal:sandbox",
        asset_canonical_id="iso4217:USD",
        amount_base_units=2500,
        destination=destination,
        required_confirmations=1,
        quote_expires_at=datetime.now(UTC) + timedelta(seconds=ends_in),
    )


def _quoted(paypal: FakePayPal) -> tuple[PayPalCheckoutAdapter, PaymentDestination]:
    adapter = _adapter(paypal)
    return adapter, adapter.prepare_quote(_quote())


# --------------------------------------------------------------------------- amounts


def test_amounts_are_cents_both_ways() -> None:
    assert usd_text(2500) == "25.00" and usd_text(5) == "0.05" and usd_text(19999) == "199.99"
    assert usd_cents("25.00") == 2500 and usd_cents("25") == 2500 and usd_cents("0.05") == 5
    for bad in ("25.001", "-1.00", "NaN", "abc", None):
        assert usd_cents(bad) is None


# --------------------------------------------------------------------------- PayPal client


def test_the_client_signs_in_once_and_never_shows_a_secret() -> None:
    paypal = FakePayPal()
    client = PayPalClient(CLIENT_ID, SECRET, live=False, transport=paypal.transport)
    client.get("/v1/customer/disputes", {"disputed_transaction_id": "X"})
    client.get("/v1/customer/disputes", {"disputed_transaction_id": "Y"})
    assert paypal.token_requests == 1  # the access token is kept until shortly before it ends
    assert "https://uri.paypal.com/services/disputes/read-seller" in client.scopes

    refused = PayPalClient(CLIENT_ID, "E" + "x" * 40, live=False, transport=paypal.transport)
    with pytest.raises(PayPalUnavailable) as caught:
        refused.access_token()
    message = str(caught.value)
    assert "refused the app's client ID and secret" in message and "HTTP 401" in message
    for secret in (SECRET, CLIENT_ID, TOKEN, "x" * 40):
        assert secret not in message


def test_paypal_errors_name_the_issue_and_never_a_token() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
        body = {
            "name": "UNPROCESSABLE_ENTITY",
            "message": f"Bearer {TOKEN} was used",
            "details": [{"issue": "INSTRUMENT_DECLINED", "description": "The card was declined."}],
            "debug_id": "abc123",
        }
        return httpx.Response(422, json=body)

    client = PayPalClient(CLIENT_ID, SECRET, live=False, transport=httpx.MockTransport(answer))
    with pytest.raises(PayPalUnavailable) as caught:
        client.post("/v2/checkout/orders/X/capture", {})
    assert caught.value.issue == "INSTRUMENT_DECLINED" and caught.value.status == 422
    assert "INSTRUMENT_DECLINED" in str(caught.value) and "abc123" in str(caught.value)
    assert "(The card was declined.)" in str(caught.value)  # PayPal's own words
    assert TOKEN not in str(caught.value)


def test_unreachable_or_busy_paypal_is_unavailable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = PayPalClient(CLIENT_ID, SECRET, live=True, transport=httpx.MockTransport(down))
    with pytest.raises(PayPalUnavailable, match="unreachable"):
        client.access_token()
    busy = FakePayPal()
    busy.refuse = (429, {"name": "RATE_LIMIT_REACHED"})
    client = PayPalClient(CLIENT_ID, SECRET, live=False, transport=busy.transport)
    with pytest.raises(PayPalUnavailable) as caught:
        client.access_token()
    assert caught.value.rate_limited


def test_live_and_sandbox_are_paypals_two_addresses() -> None:
    assert str(PayPalClient("a" * 20, "b" * 20, live=True)._client.base_url).startswith(
        "https://api-m.paypal.com"
    )
    assert str(PayPalClient("a" * 20, "b" * 20, live=False)._client.base_url).startswith(
        "https://api-m.sandbox.paypal.com"
    )


# --------------------------------------------------------------------------- the adapter


def test_without_its_credentials_the_adapter_names_only_their_variables() -> None:
    adapter = PayPalCheckoutAdapter("paypal", _rail(), {"DMD_PAYPAL_CLIENT_ID": CLIENT_ID})
    with pytest.raises(AdapterUnavailable) as caught:
        adapter.check_ready()
    assert "DMD_PAYPAL_CLIENT_SECRET" in str(caught.value) and CLIENT_ID not in str(caught.value)


def test_quote_creates_an_order_for_exactly_the_quote() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    body = paypal.created[0]
    unit = body["purchase_units"][0]
    assert body["intent"] == "CAPTURE"
    assert unit["amount"] == {"currency_code": "USD", "value": "25.00"}
    assert unit["reference_id"] == unit["custom_id"] == unit["invoice_id"] == PAYMENT
    assert unit["description"].startswith("DebitMyData DID-LLM registration (test): DID-LLM")
    context = body["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == f"{PAGE}?paypal=paid&payment={PAYMENT}"
    assert context["cancel_url"] == f"{PAGE}?paypal=back&payment={PAYMENT}"
    assert context["payment_method_preference"] == "IMMEDIATE_PAYMENT_REQUIRED"  # no eCheck
    assert context["shipping_preference"] == "NO_SHIPPING"
    assert context["user_action"] == "PAY_NOW" and context["brand_name"] == "DebitMyData"
    order_id = destination.payment_reference or ""
    assert destination.recipient_kind == "hosted_payment_page"
    assert destination.recipient == f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
    create = next(r for r in paypal.requests if r.url.path == "/v2/checkout/orders")
    assert create.headers["PayPal-Request-Id"] == f"dmd-order-{PAYMENT}"  # never two orders

    # The same quote again gives the same order.
    assert adapter.prepare_quote(_quote()).payment_reference == order_id
    assert len(paypal.orders) == 1


def test_return_link_keeps_an_existing_query() -> None:
    assert return_link("https://x.test/get-did?app=1", "paid", "p") == (
        "https://x.test/get-did?app=1&paypal=paid&payment=p"
    )


def test_quote_needs_a_page_to_come_back_to() -> None:
    adapter = _adapter(FakePayPal())
    with pytest.raises(AdapterUnavailable, match="come back"):
        adapter.prepare_quote(QuotedPayment(PAYMENT, "r", 2500, datetime.now(UTC), "d", ""))


def test_a_live_page_on_a_sandbox_rail_is_refused() -> None:
    paypal = FakePayPal(live=True)  # orders whose page is on www.paypal.com
    with pytest.raises(AdapterUnavailable, match="www.sandbox.paypal.com"):
        _quoted(paypal)


def test_prepare_destination_alone_is_not_enough() -> None:
    with pytest.raises(AdapterUnavailable):
        _adapter(FakePayPal()).prepare_destination(PAYMENT)


def test_an_unapproved_order_shows_nothing() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    observation = adapter.observe(_expected(destination))
    assert observation.outputs == () and "PAYER_ACTION_REQUIRED" in observation.notes[0]
    assert paypal.capture_requests() == []


def test_an_approved_order_is_captured_once_and_is_one_confirmed_output() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    order_id = destination.payment_reference or ""
    paypal.approve(order_id)
    observation = adapter.observe(_expected(destination))
    (output,) = observation.outputs
    capture = paypal.capture_of(order_id)
    assert output.transaction_reference == capture["id"]
    assert (output.amount_base_units, output.confirmations) == (2500, 1)
    assert not observation.refunded and not observation.disputed
    (request,) = paypal.capture_requests()
    assert request.headers["PayPal-Request-Id"].startswith(f"dmd-capture-{PAYMENT}-")

    # Checked again later: read, never captured twice.
    again = adapter.observe(_expected(destination))
    assert again.outputs == observation.outputs and len(paypal.capture_requests()) == 1


def test_an_order_approved_after_the_quote_ended_is_never_captured() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    paypal.approve(destination.payment_reference or "")
    for ends_in in (-60, 10):  # ended, or ending before the capture could count
        observation = adapter.observe(_expected(destination, ends_in=ends_in))
        assert observation.outputs == () and "nothing taken" in observation.notes[0]
    assert paypal.capture_requests() == []


def test_a_declined_way_to_pay_lets_the_payer_choose_again() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    order_id = destination.payment_reference or ""
    paypal.approve(order_id)
    paypal.decline_next_capture = True
    observation = adapter.observe(_expected(destination))
    assert observation.outputs == () and "INSTRUMENT_DECLINED" in observation.notes[0]
    # Not tried again until the payer approves again.
    waiting = adapter.observe(_expected(destination))
    assert "approve again" in waiting.notes[0] and len(paypal.capture_requests()) == 1
    # The payer approves again with another way to pay: a new capture request, which works
    # (PayPal would answer the same request ID with the first refusal).
    paypal.approve(order_id)
    assert len(adapter.observe(_expected(destination)).outputs) == 1
    first, second = paypal.capture_requests()
    assert first.headers["PayPal-Request-Id"] != second.headers["PayPal-Request-Id"]


def test_a_refused_payment_is_tried_once_per_approval(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Alembic's logging setup (migrations/env.py) turns off loggers made before it, in tests.
    monkeypatch.setattr(paypal_checkout.log, "disabled", False)
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    order_id = destination.payment_reference or ""
    paypal.approve(order_id)
    paypal.refuse_next_capture = "TRANSACTION_REFUSED"
    with caplog.at_level("WARNING"):
        refused = adapter.observe(_expected(destination))
    assert refused.outputs == () and "TRANSACTION_REFUSED" in refused.notes[0]
    logged = [
        record for record in caplog.records if record.getMessage() == "paypal refused a capture"
    ]
    assert "The requested transaction was refused." in logged[0].event["error"]  # type: ignore[attr-defined]
    for _ in range(3):  # every pass of the worker: no new capture, nothing taken
        assert adapter.observe(_expected(destination)).outputs == ()
    assert len(paypal.capture_requests()) == 1
    paypal.approve(order_id)
    assert len(adapter.observe(_expected(destination)).outputs) == 1
    assert len(paypal.capture_requests()) == 2


def test_an_order_being_approved_again_is_waited_for() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    order_id = destination.payment_reference or ""
    paypal.approve(order_id)
    paypal.refuse_next_capture = "ORDER_NOT_APPROVED"
    waiting = adapter.observe(_expected(destination))
    assert waiting.outputs == () and "not approved (yet)" in waiting.notes[0]
    paypal.approve(order_id)  # the payer finishes on PayPal's page
    assert len(adapter.observe(_expected(destination)).outputs) == 1


def test_a_held_capture_counts_once_paypal_completes_it() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    order_id = destination.payment_reference or ""
    paypal.approve(order_id)
    paypal.hold_next_capture = "PENDING_REVIEW"
    observation = adapter.observe(_expected(destination))
    assert observation.outputs == () and "PENDING (PENDING_REVIEW)" in observation.notes[0]
    paypal.complete_held(order_id)
    assert len(adapter.observe(_expected(destination)).outputs) == 1


def test_refunds_and_disputes_are_reported() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    order_id = destination.payment_reference or ""
    paypal.approve(order_id)
    adapter.observe(_expected(destination))
    paypal.refund(order_id, partly=True)
    partly = adapter.observe(_expected(destination))
    assert not partly.refunded and "partial refund" in partly.notes[0]
    paypal.dispute(order_id)
    assert adapter.observe(_expected(destination)).disputed
    paypal.refund(order_id)
    refunded = adapter.observe(_expected(destination))
    assert refunded.refunded and len(refunded.outputs) == 1  # the payment did come in


def test_a_chargeback_hold_is_a_dispute_without_the_disputes_permission() -> None:
    paypal = FakePayPal(scopes="")  # the app may not read disputes
    adapter, destination = _quoted(paypal)
    order_id = destination.payment_reference or ""
    paypal.approve(order_id)
    adapter.observe(_expected(destination))
    paypal.dispute(order_id)  # not readable without the permission
    assert not adapter.observe(_expected(destination)).disputed
    capture = paypal.capture_of(order_id)
    capture.update(status="PENDING", status_details={"reason": "CHARGEBACK"})
    held = adapter.observe(_expected(destination))
    assert held.disputed and len(held.outputs) == 1
    assert not any(r.url.path == "/v1/customer/disputes" for r in paypal.requests)


def test_an_order_of_another_payment_is_never_credited() -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    paypal.approve(destination.payment_reference or "")
    with pytest.raises(AdapterUnavailable, match="not for dmd_payment_intent_B"):
        adapter.observe(_expected(destination, payment_id="dmd_payment_intent_B"))
    assert paypal.capture_requests() == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda c: c["amount"].update(value="24.00"), "not the quoted amount"),
        (lambda c: c["amount"].update(currency_code="EUR"), "not in US dollars"),
        (lambda c: c.update(custom_id="dmd_payment_intent_Z"), "for another payment"),
        (lambda c: c.update(final_capture=False), "not the final capture"),
    ],
)
def test_an_unexpected_paypal_payment_is_not_credited(change: Any, problem: str) -> None:
    paypal = FakePayPal()
    adapter, destination = _quoted(paypal)
    order_id = destination.payment_reference or ""
    paypal.approve(order_id)
    adapter.observe(_expected(destination))
    change(paypal.capture_of(order_id))
    with pytest.raises(AdapterUnavailable, match=problem):
        adapter.observe(_expected(destination))


# --------------------------------------------------------------------------- registry


def test_shipped_registries_pay_with_live_paypal_in_production_and_its_sandbox_on_staging() -> None:
    production = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    staging = load_registry(REGTEST_PATH, environ={}, available_adapters=set())
    assert production.rails["paypal"].network == "paypal:live"
    assert staging.rails["paypal"].network == "paypal:sandbox"
    for registry in (production, staging):
        assert registry.rail_state("paypal") is RailState.DISABLED
        assert "paypal" in registry.services["did_registration"].payment_options
        for product in registry.products.values():
            assert "paypal" not in product.payment_options


def test_paypal_switched_on_without_its_credentials_stops_the_start() -> None:
    environ = {"DMD_RAIL_PAYPAL_ENABLED": "true", "DMD_PAYPAL_CLIENT_ID": CLIENT_ID}
    with pytest.raises(RegistryError) as caught:
        load_registry(REGTEST_PATH, environ=environ, available_adapters={"PayPalCheckoutAdapter"})
    assert "DMD_PAYPAL_CLIENT_SECRET is empty" in str(caught.value)
    environ["DMD_PAYPAL_CLIENT_SECRET"] = CLIENT_ID
    with pytest.raises(RegistryError, match="is the client ID, not the app's secret") as caught:
        load_registry(REGTEST_PATH, environ=environ, available_adapters={"PayPalCheckoutAdapter"})
    assert CLIENT_ID not in str(caught.value)


def test_a_product_cannot_be_paid_with_paypal_yet(tmp_path: Path) -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    product = next(iter(data["products"].values()))
    product["payment_options"] = ["paypal"]
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RegistryError, match="only built for DID-LLM registrations"):
        load_registry(path, environ={}, available_adapters=set())


# --------------------------------------------------------------------------- with a database

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


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
def paypal_client(engine: Engine, tmp_path: Path) -> Iterator[TestClient]:
    PAYPAL.__init__()  # a fresh PayPal for every test
    data = yaml.safe_load(REGTEST_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings = Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        route_registry_path=path,
        app_host="id.example.test",
    )
    app = create_app(
        settings,
        environ=PAYPAL_ENV,
        engine=engine,
        adapters={"PayPalCheckoutAdapter": FakePayPalAdapter},
    )
    with TestClient(app) as client:
        yield client


def _paypal_quote(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/v1/dids/registration-quotes",
        json={
            "display_name": "PayPal Test Agent",
            "participant_type": "autonomous_agent",
            "controller_public_key": f"ed25519:{AGENT_PUBLIC_HEX}",
            "preferred_payment_option": "paypal",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pass(client: TestClient, now: datetime) -> None:
    state = client.app.state  # type: ignore[attr-defined]
    run_pass(session_factory(state.engine), state.quote_context.adapters, now)


def _status(engine: Engine, payment_id: str) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status FROM payment_intents WHERE id = :id"), {"id": payment_id}
        ).scalar_one()


@needs_db
def test_paypal_registration_quote_capture_and_refund(
    paypal_client: TestClient, engine: Engine
) -> None:
    before = datetime.now(UTC)
    quote = _paypal_quote(paypal_client)
    option = quote["selected_payment_option"]
    payment_id = quote["payment_intent_id"]
    order_id = option["payment_reference"]
    assert option["payment_protocol"] == "paypal_checkout"
    assert (option["asset"], option["amount"], option["network"]) == (
        "USD",
        "25.00",
        "paypal:sandbox",
    )
    assert option["payment_url"] == option["recipient"]
    assert option["payment_url"].startswith("https://www.sandbox.paypal.com/checkoutnow?token=")
    assert quote["next_action"] == "pay_at_payment_url"
    expires = datetime.fromisoformat(quote["expires_at"])
    assert timedelta(minutes=30) < expires - before <= timedelta(minutes=31, seconds=5)
    context = PAYPAL.created[0]["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == f"https://id.example.test/?paypal=paid&payment={payment_id}"

    _pass(paypal_client, datetime.now(UTC))
    assert _status(engine, payment_id) == "awaiting_payment"

    PAYPAL.approve(order_id)
    _pass(paypal_client, datetime.now(UTC))
    assert _status(engine, payment_id) == "settled"
    status = paypal_client.get(f"/v1/payments/{payment_id}").json()
    assert status["transactions"] == [PAYPAL.capture_of(order_id)["id"]]
    assert status["amount_received"] == "25.00"
    assert status["next_action"] == "prove_wallet_control"

    PAYPAL.refund(order_id)
    state = paypal_client.app.state  # type: ignore[attr-defined]
    later = datetime.now(UTC) + timedelta(minutes=16)
    run_pass(session_factory(state.engine), state.quote_context.adapters, later)
    assert _status(engine, payment_id) == "refunded"


@needs_db
def test_a_paypal_approval_after_the_quote_ended_takes_nothing(
    paypal_client: TestClient, engine: Engine
) -> None:
    quote = _paypal_quote(paypal_client)
    PAYPAL.approve(quote["selected_payment_option"]["payment_reference"])
    # The quote ended a minute ago (the adapter goes by the real time).
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE payment_intents SET quote_expires_at = :at WHERE id = :id"),
            {"at": datetime.now(UTC) - timedelta(minutes=1), "id": quote["payment_intent_id"]},
        )
    _pass(paypal_client, datetime.now(UTC))
    assert _status(engine, quote["payment_intent_id"]) == "expired"
    assert PAYPAL.capture_requests() == []


@needs_db
def test_paypal_down_means_no_paypal_quote(paypal_client: TestClient) -> None:
    PAYPAL.refuse = (503, {"name": "SERVICE_UNAVAILABLE"})
    response = paypal_client.post(
        "/v1/dids/registration-quotes",
        json={
            "display_name": "PayPal Test Agent",
            "participant_type": "autonomous_agent",
            "controller_public_key": f"ed25519:{AGENT_PUBLIC_HEX}",
            "preferred_payment_option": "paypal",
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_rail_unavailable"
    assert SECRET not in response.text and CLIENT_ID not in response.text


@needs_db
def test_agents_and_people_are_told_a_person_pays_with_paypal(paypal_client: TestClient) -> None:
    llms = paypal_client.get("/llms.txt").text
    assert "payment option `paypal`" in llms and "PayPal's payment page" in llms
    page = paypal_client.get("/get-did").text
    assert "PayPal's payment page" in page
    options = paypal_client.get("/v1/payment-options?service=did_registration").json()
    paypal = next(o for o in options["supported_payment_options"] if o["id"] == "paypal")
    assert paypal["status"] == "enabled" and paypal["payment_protocol"] == "paypal_checkout"
    assert json.dumps(options).count(SECRET) == 0


def test_the_checkout_says_how_a_person_can_pay_right_now() -> None:
    from dmd_gateway.mcp_server import _ways_to_pay

    card, paypal, dmd = (
        {"payment_protocol": "stripe_checkout"},
        {"payment_protocol": "paypal_checkout"},
        {"payment_protocol": "dmd_native"},
    )
    assert _ways_to_pay([dmd]) == "from your own wallet"
    assert _ways_to_pay([dmd, card]) == "by card or from your own wallet"
    assert _ways_to_pay([paypal, dmd, card]) == "by card, with PayPal or from your own wallet"
