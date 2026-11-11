"""Card payments through Stripe Checkout: the Stripe client, the adapter, the settlement rules
and, with a database, a whole card registration (quote, payment, refund).

Stripe itself is replaced by tests/stripe_fakes.py: nothing here reaches the network.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.adapters.base import (
    AdapterUnavailable,
    ExpectedPayment,
    PaymentDestination,
    PaymentObservation,
    QuotedPayment,
)
from dmd_gateway.adapters.stripe_api import StripeClient, StripeUnavailable, form_fields
from dmd_gateway.adapters.stripe_checkout import StripeCheckoutAdapter, return_link
from dmd_gateway.db.session import session_factory
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.domain.routes import load_registry
from dmd_gateway.domain.settlement import SettlementContext, plan
from dmd_gateway.main import create_app
from dmd_gateway.services.settlement import due_intent_ids, run_pass
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, REPO_ROOT, TEST_DATABASE_URL, alembic_config
from .fakes import AGENT_PUBLIC_HEX
from .stripe_fakes import STRIPE, TEST_KEY, FakeStripe, FakeStripeAdapter, form

REGTEST_PATH = REPO_ROOT / "config" / "payment_routes.regtest.yaml"
CARD_ENV = {"DMD_RAIL_CARD_STRIPE_ENABLED": "true", "DMD_STRIPE_SECRET_KEY": TEST_KEY}
PAGE = "https://id.example.test/"
S = PaymentStatus


def _rail() -> Any:
    registry = load_registry(
        REGTEST_PATH, environ=CARD_ENV, available_adapters={"StripeCheckoutAdapter"}
    )
    return registry.rails["card_stripe"]


def _adapter(stripe: FakeStripe) -> StripeCheckoutAdapter:
    client = StripeClient(TEST_KEY, transport=stripe.transport)
    return StripeCheckoutAdapter("card_stripe", _rail(), CARD_ENV, client=client)


def _quote(payment_id: str = "dmd_payment_intent_A", expires_in: int = 1860) -> QuotedPayment:
    return QuotedPayment(
        payment_intent_id=payment_id,
        registration_intent_id="dmd_did_intent_A",
        amount_base_units=2500,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        product_description="DID-LLM “Test”, registration dmd_did_intent_A",
        return_url=PAGE,
    )


def _expected(destination: PaymentDestination, payment_id: str = "dmd_payment_intent_A"):
    return ExpectedPayment(
        payment_intent_id=payment_id,
        network="stripe:test",
        asset_canonical_id="iso4217:USD",
        amount_base_units=2500,
        destination=destination,
        required_confirmations=1,
    )


# --------------------------------------------------------------------------- Stripe client


def test_form_encoding_follows_stripes_nested_style() -> None:
    fields = form_fields(
        {
            "mode": "payment",
            "line_items": [{"quantity": 1, "price_data": {"unit_amount": 2500}}],
            "allowed_payment_method_types": ["card"],
            "metadata": {"a": "b"},
            "skip": None,
            "flag": True,
        }
    )
    assert fields == [
        ("mode", "payment"),
        ("line_items[0][quantity]", "1"),
        ("line_items[0][price_data][unit_amount]", "2500"),
        ("allowed_payment_method_types[0]", "card"),
        ("metadata[a]", "b"),
        ("flag", "true"),
    ]


def test_stripe_errors_never_show_a_key() -> None:
    stripe = FakeStripe()
    stripe.refuse = (
        401,
        {
            "error": {
                "type": "invalid_request_error",
                "message": "Invalid API Key provided: rk_test_****kkkk",
            }
        },
    )
    client = StripeClient(TEST_KEY, transport=stripe.transport)
    with pytest.raises(StripeUnavailable) as raised:
        client.get("/v1/charges", {"limit": 1})
    message = str(raised.value)
    assert "HTTP 401" in message and "invalid_request_error" in message
    assert "rk_test" not in message and TEST_KEY not in message
    assert not raised.value.rate_limited


def test_unreachable_stripe_is_unavailable() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = StripeClient(TEST_KEY, transport=httpx.MockTransport(fail))
    with pytest.raises(StripeUnavailable, match="unreachable"):
        client.get("/v1/charges")


# --------------------------------------------------------------------------- the adapter


def test_check_ready_reads_sessions_payment_intents_and_charges() -> None:
    stripe = FakeStripe()
    _adapter(stripe).check_ready()
    assert [r.url.path for r in stripe.requests] == [
        "/v1/checkout/sessions",
        "/v1/payment_intents",
        "/v1/charges",
    ]


def test_a_key_without_a_permission_is_reported() -> None:
    stripe = FakeStripe()
    stripe.refuse = (403, {"error": {"type": "invalid_request_error", "code": "permission"}})
    with pytest.raises(AdapterUnavailable, match="HTTP 403"):
        _adapter(stripe).check_ready()


def test_too_many_requests_is_a_rate_limit() -> None:
    stripe = FakeStripe()
    stripe.refuse = (429, {"error": {"type": "rate_limit_error"}})
    with pytest.raises(AdapterUnavailable) as raised:
        _adapter(stripe).check_ready()
    assert raised.value.rate_limited


@pytest.mark.parametrize("key", ["sk_test_" + "k" * 24, "rk_live_" + "k" * 24, ""])
def test_the_adapter_only_uses_a_restricted_key_of_its_mode(key: str) -> None:
    adapter = StripeCheckoutAdapter(
        "card_stripe", _rail(), {**CARD_ENV, "DMD_STRIPE_SECRET_KEY": key}
    )
    with pytest.raises(AdapterUnavailable, match="DMD_STRIPE_SECRET_KEY is not a restricted") as e:
        adapter.check_ready()
    assert not key or key not in str(e.value)


def test_quote_creates_a_payment_page_for_exactly_the_quote() -> None:
    stripe = FakeStripe()
    quote = _quote()
    destination = _adapter(stripe).prepare_quote(quote)
    session = stripe.sessions[destination.payment_reference or ""]
    assert destination.recipient_kind == "hosted_payment_page"
    assert destination.recipient == session["url"]
    assert destination.recipient.startswith("https://checkout.stripe.com/")
    create = stripe.requests[-1]
    assert create.headers["Idempotency-Key"] == "dmd-checkout-dmd_payment_intent_A"
    fields = form(create)
    assert fields["mode"] == "payment"
    assert fields["line_items[0][quantity]"] == "1"
    assert fields["line_items[0][price_data][currency]"] == "usd"
    assert fields["line_items[0][price_data][unit_amount]"] == "2500"
    assert fields["line_items[0][price_data][product_data][name]"] == (
        "DebitMyData DID-LLM registration (test)"
    )
    assert fields["allowed_payment_method_types[0]"] == "card"
    # Always in US dollars: Stripe's prices in the payer's own currency are off.
    assert fields["adaptive_pricing[enabled]"] == "false"
    assert "allowed_payment_method_types[1]" not in fields
    assert fields["client_reference_id"] == "dmd_payment_intent_A"
    assert fields["metadata[payment_intent_id]"] == "dmd_payment_intent_A"
    assert fields["payment_intent_data[metadata][payment_intent_id]"] == "dmd_payment_intent_A"
    assert fields["metadata[registration_intent_id]"] == "dmd_did_intent_A"
    # The page closes when the quote does, so no card is charged for an expired quote.
    assert fields["expires_at"] == str(int(quote.expires_at.timestamp()))
    assert fields["success_url"] == f"{PAGE}?card=paid&payment=dmd_payment_intent_A"
    assert fields["cancel_url"] == f"{PAGE}?card=back&payment=dmd_payment_intent_A"


def test_return_link_keeps_an_existing_query() -> None:
    assert return_link("https://a.test/get-did?x=1", "paid", "p") == (
        "https://a.test/get-did?x=1&card=paid&payment=p"
    )


def test_quote_needs_a_page_to_come_back_to() -> None:
    quote = replace(_quote(), return_url="")
    with pytest.raises(AdapterUnavailable, match="come back"):
        _adapter(FakeStripe()).prepare_quote(quote)


def test_a_live_session_on_a_test_rail_is_refused() -> None:
    stripe = FakeStripe(livemode=True)
    with pytest.raises(AdapterUnavailable, match="other mode"):
        _adapter(stripe).prepare_quote(_quote())


def test_prepare_destination_alone_is_not_enough() -> None:
    with pytest.raises(AdapterUnavailable):
        _adapter(FakeStripe()).prepare_destination("dmd_payment_intent_A")


def test_unpaid_page_shows_nothing() -> None:
    stripe = FakeStripe()
    adapter = _adapter(stripe)
    destination = adapter.prepare_quote(_quote())
    observation = adapter.observe(_expected(destination))
    assert observation.outputs == ()
    assert "not paid" in observation.notes[0]
    stripe.expire(destination.payment_reference or "")
    assert adapter.observe(_expected(destination)).outputs == ()


def test_paid_page_is_one_confirmed_output() -> None:
    stripe = FakeStripe()
    adapter = _adapter(stripe)
    destination = adapter.prepare_quote(_quote())
    intent_id = stripe.pay(destination.payment_reference or "", created=1_800_000_000)
    observation = adapter.observe(_expected(destination))
    (output,) = observation.outputs
    assert output.transaction_reference == intent_id
    assert output.amount_base_units == 2500
    assert output.confirmations == 1
    assert output.first_seen_at == datetime.fromtimestamp(1_800_000_000, UTC)
    assert not observation.refunded and not observation.disputed
    read = stripe.requests[-1]
    assert read.url.params.get("expand[0]") == "payment_intent.latest_charge"


def test_refunds_and_disputes_are_reported() -> None:
    stripe = FakeStripe()
    adapter = _adapter(stripe)
    destination = adapter.prepare_quote(_quote())
    session_id = destination.payment_reference or ""
    stripe.pay(session_id)
    stripe.refund(session_id, cents=500)
    partial = adapter.observe(_expected(destination))
    assert not partial.refunded
    assert "500 cents refunded" in partial.notes[0]
    stripe.refund(session_id)
    assert adapter.observe(_expected(destination)).refunded
    stripe.dispute(session_id)
    assert adapter.observe(_expected(destination)).disputed


def test_a_page_of_another_payment_is_never_credited() -> None:
    stripe = FakeStripe()
    adapter = _adapter(stripe)
    destination = adapter.prepare_quote(_quote("dmd_payment_intent_OTHER"))
    stripe.pay(destination.payment_reference or "")
    with pytest.raises(AdapterUnavailable, match="is not for dmd_payment_intent_A"):
        adapter.observe(_expected(destination, "dmd_payment_intent_A"))


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda charge, intent: charge.update(status="pending"), "charge pending"),
        (lambda charge, intent: charge.update(captured=False), "not captured"),
        (lambda charge, intent: charge.update(currency="eur"), "not in US dollars"),
        (lambda charge, intent: intent.update(status="processing"), "PaymentIntent processing"),
    ],
)
def test_an_unexpected_card_payment_is_not_credited(change: Any, problem: str) -> None:
    stripe = FakeStripe()
    adapter = _adapter(stripe)
    destination = adapter.prepare_quote(_quote())
    intent_id = stripe.pay(destination.payment_reference or "")
    intent = stripe.intents[intent_id]
    change(stripe.charges[intent["latest_charge"]], intent)
    with pytest.raises(AdapterUnavailable, match=problem):
        adapter.observe(_expected(destination))


# --------------------------------------------------------------------------- settlement rules


def _observation(*, refunded: bool = False, disputed: bool = False) -> PaymentObservation:
    stripe = FakeStripe()
    adapter = _adapter(stripe)
    destination = adapter.prepare_quote(_quote())
    session_id = destination.payment_reference or ""
    stripe.pay(session_id, created=int(datetime.now(UTC).timestamp()))
    if refunded:
        stripe.refund(session_id)
    if disputed:
        stripe.dispute(session_id)
    return adapter.observe(_expected(destination))


def _ctx() -> SettlementContext:
    now = datetime.now(UTC)
    return SettlementContext(2500, 1, now + timedelta(minutes=30), now)


def test_a_card_payment_settles_in_one_pass() -> None:
    steps = plan(S.AWAITING_PAYMENT, _observation(), _ctx())
    assert [step.to_status for step in steps] == [S.DETECTED, S.CONFIRMING, S.VERIFIED, S.SETTLED]


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [({"refunded": True}, S.REFUNDED), ({"disputed": True}, S.DISPUTED)],
)
def test_a_settled_card_payment_taken_back(kwargs: dict[str, bool], status: S) -> None:
    steps = plan(S.SETTLED, _observation(**kwargs), _ctx())
    assert [step.to_status for step in steps] == [status]
    # Refunded before the worker even saw it settle: it settles, then is refunded.
    steps = plan(S.AWAITING_PAYMENT, _observation(**kwargs), _ctx())
    assert steps[-2].to_status is S.SETTLED and steps[-1].to_status is status


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
def card_client(engine: Engine, tmp_path: Path) -> Iterator[TestClient]:
    STRIPE.__init__()  # a fresh Stripe for every test
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
        environ=CARD_ENV,
        engine=engine,
        adapters={"StripeCheckoutAdapter": FakeStripeAdapter},
    )
    with TestClient(app) as client:
        yield client


def _card_quote(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/v1/dids/registration-quotes",
        json={
            "display_name": "Card Test Agent",
            "participant_type": "autonomous_agent",
            "controller_public_key": f"ed25519:{AGENT_PUBLIC_HEX}",
            "preferred_payment_option": "card_stripe",
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
def test_card_registration_quote_payment_and_refund(
    card_client: TestClient, engine: Engine
) -> None:
    before = datetime.now(UTC)
    quote = _card_quote(card_client)
    option = quote["selected_payment_option"]
    payment_id = quote["payment_intent_id"]
    session_id = option["payment_reference"]
    assert option["payment_protocol"] == "stripe_checkout"
    assert (option["asset"], option["amount"], option["amount_base_units"]) == (
        "USD",
        "25.00",
        "2500",
    )
    assert option["network"] == "stripe:test"
    assert option["payment_url"] == option["recipient"] == STRIPE.sessions[session_id]["url"]
    assert quote["next_action"] == "pay_at_payment_url"
    assert quote["price_quote"] is None
    # A card quote lasts 31 minutes: Stripe's payment page stays open 30 at least.
    expires = datetime.fromisoformat(quote["expires_at"])
    assert timedelta(minutes=30) < expires - before <= timedelta(minutes=31, seconds=5)
    assert STRIPE.created[0]["success_url"] == (
        f"https://id.example.test/?card=paid&payment={payment_id}"
    )

    status = card_client.get(f"/v1/payments/{payment_id}").json()
    assert status["next_action"] == "pay_at_payment_url"
    assert status["payment_url"] == option["payment_url"]
    registration = card_client.get(
        f"/v1/dids/registrations/{quote['registration_intent_id']}"
    ).json()
    assert registration["next_action"] == "pay_at_payment_url"

    _pass(card_client, datetime.now(UTC))
    assert _status(engine, payment_id) == "awaiting_payment"

    intent_id = STRIPE.pay(session_id, created=int(datetime.now(UTC).timestamp()))
    _pass(card_client, datetime.now(UTC))
    assert _status(engine, payment_id) == "settled"
    status = card_client.get(f"/v1/payments/{payment_id}").json()
    assert status["status"] == "settled"
    assert status["payment_url"] is None
    assert status["transactions"] == [intent_id]
    assert status["amount_received"] == "25.00"
    assert status["next_action"] == "prove_wallet_control"

    # A settled card payment is checked again only every 15 minutes, for refunds and disputes.
    state = card_client.app.state  # type: ignore[attr-defined]
    soon = datetime.now(UTC) + timedelta(minutes=5)
    with session_factory(engine)() as session:
        due = due_intent_ids(
            session, ["card_stripe"], soon, recheck_intervals={"card_stripe": timedelta(minutes=15)}
        )
    assert payment_id not in due

    STRIPE.refund(session_id)
    later = datetime.now(UTC) + timedelta(minutes=16)
    run_pass(session_factory(state.engine), state.quote_context.adapters, later)
    assert _status(engine, payment_id) == "refunded"
    registration = card_client.get(
        f"/v1/dids/registrations/{quote['registration_intent_id']}"
    ).json()
    assert registration["status"] == "cancelled"
    assert registration["next_action"] == "request_new_quote"


@needs_db
def test_an_unpaid_card_quote_expires(card_client: TestClient, engine: Engine) -> None:
    quote = _card_quote(card_client)
    _pass(card_client, datetime.now(UTC) + timedelta(minutes=32))
    assert _status(engine, quote["payment_intent_id"]) == "expired"


@needs_db
def test_stripe_down_means_no_card_quote(card_client: TestClient) -> None:
    STRIPE.refuse = (500, {"error": {"type": "api_error"}})
    response = card_client.post(
        "/v1/dids/registration-quotes",
        json={
            "display_name": "Card Test Agent",
            "participant_type": "autonomous_agent",
            "controller_public_key": f"ed25519:{AGENT_PUBLIC_HEX}",
            "preferred_payment_option": "card_stripe",
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_rail_unavailable"


@needs_db
def test_llms_txt_tells_agents_a_person_pays_by_card(card_client: TestClient) -> None:
    text_ = card_client.get("/llms.txt").text
    assert "payment option `card_stripe`" in text_
    assert "Give the link to your human" in text_


def test_shipped_registry_offers_cards_for_registrations_only() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    assert "card_stripe" in registry.services["did_registration"].payment_options
    for product in registry.products.values():
        assert "card_stripe" not in product.payment_options
    for donation in registry.donations.values():
        assert "card_stripe" not in donation.payment_options


@needs_db
def test_a_dispute_after_issuance_raises_an_alert(card_client: TestClient, engine: Engine) -> None:
    quote = _card_quote(card_client)
    payment_id = quote["payment_intent_id"]
    session_id = quote["selected_payment_option"]["payment_reference"]
    STRIPE.pay(session_id, created=int(datetime.now(UTC).timestamp()))
    _pass(card_client, datetime.now(UTC))
    assert _status(engine, payment_id) == "settled"
    # As if the DID-LLM had been issued (issuance itself is tested elsewhere).
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE registration_intents SET status = 'issued' WHERE id = :id"),
            {"id": quote["registration_intent_id"]},
        )
    STRIPE.dispute(session_id)
    _pass(card_client, datetime.now(UTC) + timedelta(minutes=16))
    assert _status(engine, payment_id) == "disputed"
    with engine.connect() as conn:
        alert = conn.execute(
            text("SELECT data FROM audit_events WHERE action = :action AND subject_id = :id"),
            {
                "action": "alert.issued_identity_payment_taken_back",
                "id": quote["registration_intent_id"],
            },
        ).scalar_one()
        status = conn.execute(
            text("SELECT status FROM registration_intents WHERE id = :id"),
            {"id": quote["registration_intent_id"]},
        ).scalar_one()
    assert alert == {"payment_intent_id": payment_id, "payment_status": "disputed"}
    assert status == "issued"  # not revoked automatically: a dispute can still be won
