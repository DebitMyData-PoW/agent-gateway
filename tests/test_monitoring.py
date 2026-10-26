"""Monitoring: alert channels, de-duplication, health checks, the daily reconciliation report
and the monitor command.

Channels are tested against a fake SMTP server and a mocked Discord webhook; checks and the
report run against PostgreSQL (TEST_DATABASE_URL), with the real x402 adapter on the fake
Base chain of ``tests/x402_fakes`` for the end-to-end reconciliation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from dmd_gateway.adapters.base import AdapterUnavailable
from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import AlertState, PaymentIntent, PriceRecord, X402Authorization
from dmd_gateway.db.session import session_factory, transaction
from dmd_gateway.domain.routes import load_registry
from dmd_gateway.monitoring import checks as mc
from dmd_gateway.monitoring.alerts import (
    AlertDispatcher,
    DeliveryFailed,
    DiscordNotifier,
    EmailNotifier,
    Finding,
    Message,
    NotifyError,
    Severity,
    notifiers_from_settings,
    redact,
)
from dmd_gateway.monitoring.cli import run
from dmd_gateway.monitoring.reconcile import build_report
from dmd_gateway.monitoring.state import (
    AUDIT_CURSOR,
    RECEIVING,
    WORKER_HEARTBEAT,
    get_state,
    put_state,
)
from dmd_gateway.runtime import Runtime
from dmd_gateway.settings import Settings
from dmd_gateway.worker import _heartbeat

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL, UNREACHABLE_DB, alembic_config
from .test_donations import ADAPTERS, ENVIRON, INFERENCE, ISSUER, PRICE_SOURCE
from .test_x402_payments import prove, quote, register
from .x402_fakes import CHAIN, MERCHANT, NETWORK, TOKEN, TestPayer, reset_chain

C, W = Severity.CRITICAL, Severity.WARNING
NOW = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
WEBHOOK = "https://discord.com/api/webhooks/123/secret-token"
needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


# --------------------------------------------------------------------------- doubles


@dataclass
class FakeNotifier:
    name: str = "fake"
    fail: bool = False
    messages: list[Message] = field(default_factory=list)

    def send(self, message: Message) -> None:
        if self.fail:
            raise NotifyError("fake: down")
        self.messages.append(message)


class FakeSmtp:
    instances: list[FakeSmtp] = []

    def __init__(self, host: str, port: int, timeout: float, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.sent: list[Any] = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *args: object) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message: Any) -> None:
        self.sent.append(message)


def email_notifier(**changes: Any) -> EmailNotifier:
    FakeSmtp.instances.clear()
    values: dict[str, Any] = {
        "host": "smtp.test",
        "port": 587,
        "security": "starttls",
        "username": "alerts@example.com",
        "password": "pw",
        "sender": "alerts@example.com",
        "to_all": ["ops@example.com"],
        "to_critical": ["manager@example.com"],
        "to_report": ["ops@example.com", "manager@example.com"],
        "smtp": FakeSmtp,
        "smtp_ssl": FakeSmtp,
    }
    return EmailNotifier(**{**values, **changes})


def dispatcher(notifier: FakeNotifier | None = None) -> AlertDispatcher:
    return AlertDispatcher(
        [notifier] if notifier else [], "staging", timedelta(hours=1), timedelta(hours=24)
    )


# --------------------------------------------------------------------------- channels


def test_urls_are_cut_to_their_host() -> None:
    text_ = "RPC https://base-mainnet.g.alchemy.com/v2/SECRETKEY failed; see http://10.0.0.5:8332/"
    assert redact(text_) == "RPC https://base-mainnet.g.alchemy.com/… failed; see http://10.0.0.5/…"


def test_email_goes_to_the_right_people() -> None:
    notifier = email_notifier()
    assert notifier.recipients(Message("s", "b")) == ["ops@example.com"]
    assert notifier.recipients(Message("s", "b", critical=True)) == [
        "ops@example.com",
        "manager@example.com",
    ]
    assert notifier.recipients(Message("s", "b", report=True)) == [
        "ops@example.com",
        "manager@example.com",
    ]
    notifier.send(Message("[DMD Gateway staging] CRITICAL: x", "body", critical=True))
    (smtp,) = FakeSmtp.instances
    assert smtp.calls == ["starttls", "login:alerts@example.com:pw", "quit"]
    (sent,) = smtp.sent
    assert sent["To"] == "ops@example.com, manager@example.com"
    assert sent["Subject"] == "[DMD Gateway staging] CRITICAL: x"


def test_email_over_ssl_and_failures() -> None:
    notifier = email_notifier(security="ssl")
    notifier.send(Message("s", "b"))
    assert "starttls" not in FakeSmtp.instances[0].calls
    assert "context" in FakeSmtp.instances[0].kwargs

    class Refusing(FakeSmtp):
        def login(self, user: str, password: str) -> None:
            raise OSError("authentication failed for https://mail.test/path?key=1")

    with pytest.raises(NotifyError, match="mail.test/…") as caught:
        email_notifier(smtp=Refusing).send(Message("s", "b"))
    assert "key=1" not in str(caught.value)


def discord(handler: Any, mention: str = "123456789012345678") -> DiscordNotifier:
    return DiscordNotifier(WEBHOOK, mention, httpx.Client(transport=httpx.MockTransport(handler)))


def test_discord_mentions_only_for_critical_alerts() -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(204)

    notifier = discord(handler)
    notifier.send(Message("Critical", "body", critical=True))
    notifier.send(Message("Warning", "body"))
    notifier.send(Message("Report", "x" * 3000, critical=True, report=True))
    assert posted[0]["content"].startswith("<@123456789012345678> **Critical**")
    assert posted[0]["allowed_mentions"] == {"parse": [], "users": ["123456789012345678"]}
    assert posted[1]["content"].startswith("**Warning**")
    assert posted[1]["allowed_mentions"] == {"parse": [], "users": []}
    assert len(posted[2]["content"]) <= 2000 and "<@" not in posted[2]["content"]

    with pytest.raises(NotifyError, match="HTTP 429"):
        discord(lambda request: httpx.Response(429)).send(Message("s", "b"))
    with pytest.raises(ValueError, match="webhook"):
        DiscordNotifier("https://evil.test/api/webhooks/1/x", "", httpx.Client())


def test_channels_come_from_the_settings() -> None:
    base = Settings(_env_file=None)
    assert notifiers_from_settings(base, {}) == []
    configured = Settings(
        _env_file=None,
        alert_smtp_host="smtp.test",
        alert_smtp_from="alerts@example.com",
        alert_smtp_username="alerts@example.com",
        alert_email_to="ops@example.com",
        alert_discord_mention_user_id="123456789012345678",
    )
    environ = {"DMD_ALERT_SMTP_PASSWORD": "pw", "DMD_ALERT_DISCORD_WEBHOOK_URL": WEBHOOK}
    names = [n.name for n in notifiers_from_settings(configured, environ)]
    assert names == ["email", "discord"]
    no_sender = configured.model_copy(update={"alert_smtp_from": ""})
    with pytest.raises(ValueError, match="SMTP_FROM"):
        notifiers_from_settings(no_sender, environ)
    # A password without its login would never log in (530 "Authentication required").
    no_login = configured.model_copy(update={"alert_smtp_username": ""})
    with pytest.raises(ValueError, match="DMD_GW_ALERT_SMTP_USERNAME is empty"):
        notifiers_from_settings(no_login, environ)


def test_smtp_refusals_say_what_to_fix() -> None:
    import smtplib

    class NoLogin(FakeSmtp):
        def send_message(self, message: Any) -> None:
            raise smtplib.SMTPSenderRefused(530, b"5.7.0 Authentication required", "a@b.c")

    class WrongPassword(FakeSmtp):
        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.0 Mailgun is not loving your login")

    with pytest.raises(NotifyError, match="set DMD_GW_ALERT_SMTP_USERNAME"):
        email_notifier(smtp=NoLogin, username="").send(Message("s", "b"))
    with pytest.raises(NotifyError, match="refused the login and password"):
        email_notifier(smtp=WrongPassword).send(Message("s", "b"))


@pytest.mark.parametrize(
    ("changes", "environ", "message"),
    [
        # A comment written after a value becomes part of it (systemd environment files).
        ({"alert_email_to": "ops@example.com   # every alert"}, {}, "ALERT_EMAIL_TO"),
        ({"alert_email_critical_to": "boss"}, {}, "ALERT_EMAIL_CRITICAL_TO"),
        ({"alert_smtp_host": "smtp.example.com   # host"}, {}, "SMTP_HOST"),
        ({"alert_smtp_port": "587   # port"}, {}, "SMTP_PORT"),
        ({"alert_smtp_security": "tls"}, {}, "SMTP_SECURITY"),
        ({"alert_discord_mention_user_id": "12345678901234567890123"}, {}, "MENTION_USER_ID"),
        ({}, {"DMD_ALERT_DISCORD_WEBHOOK_URL": WEBHOOK + "   # my channel"}, "contains a space"),
    ],
)
def test_wrong_alert_settings_are_named_and_never_stop_the_gateway(
    changes: dict[str, str], environ: dict[str, str], message: str
) -> None:
    values = {
        "alert_smtp_host": "smtp.example.com",
        "alert_smtp_from": "alerts@example.com",
        "alert_email_to": "ops@example.com",
        **changes,
    }
    settings = Settings(_env_file=None, **values)  # the API and the worker still start
    with pytest.raises(ValueError, match=message):
        notifiers_from_settings(settings, environ)


def test_the_monitor_names_a_wrong_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dmd_gateway.monitoring import cli

    wrong = Settings(_env_file=None, alert_smtp_host="x", alert_email_to="ops@example.com # x")
    monkeypatch.setattr(cli, "get_settings", lambda: wrong)
    monkeypatch.setattr(cli, "runtime_environ", lambda: {})
    assert cli.main(["test"]) == 2
    assert "DMD_GW_ALERT_EMAIL_TO" in capsys.readouterr().out


# --------------------------------------------------------------------------- database


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
def db(engine: Engine) -> Iterator[Session]:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM alert_states"))
        conn.execute(text("DELETE FROM monitor_state"))
    factory = session_factory(engine)
    with factory() as session:
        session.begin()
        yield session
        session.rollback()  # nothing a test writes here outlives it


# --------------------------------------------------------------------------- de-duplication


@needs_db
def test_a_condition_is_sent_repeated_and_resolved(db: Session) -> None:
    notifier = FakeNotifier()
    alerts = dispatcher(notifier)
    stopped = Finding("worker.stopped", C, "The payment worker has stopped", "Last: never")

    first = alerts.apply(db, [stopped], {"worker"}, NOW)
    assert first is not None and first.critical
    assert first.subject == "[DMD Gateway staging] CRITICAL: The payment worker has stopped"
    assert "Last: never" in first.body
    assert alerts.apply(db, [stopped], {"worker"}, NOW + timedelta(minutes=30)) is None
    again = alerts.apply(db, [stopped], {"worker"}, NOW + timedelta(minutes=61))
    assert again is not None and "(still ongoing)" in again.subject
    assert "Ongoing since 2026-10-27 07:00 UTC" in again.body

    # The check could not run: the alert stays open.
    assert alerts.apply(db, [], {"api"}, NOW + timedelta(minutes=62)) is None
    resolved = alerts.apply(db, [], {"worker"}, NOW + timedelta(minutes=63))
    assert resolved is not None and not resolved.critical
    assert resolved.subject.endswith("Resolved: The payment worker has stopped")
    assert len(notifier.messages) == 3
    row = db.get(AlertState, "worker.stopped")
    assert row is not None and row.resolved_at is not None and row.notifications == 3


@needs_db
def test_events_are_sent_once_and_findings_go_out_together(db: Session) -> None:
    notifier = FakeNotifier()
    alerts = dispatcher(notifier)
    findings = [
        Finding("audit.1", C, "A DID's payment left the chain", kind="event"),
        Finding("price.stale", W, "The DMD price is not being updated"),
        Finding("audit.2", W, "A payment needs a refund", kind="event"),
    ]
    message = alerts.apply(db, findings, {"audit", "price"}, NOW)
    assert message is not None and message.critical
    assert message.subject == "[DMD Gateway staging] CRITICAL: 3 alerts"
    assert message.body.index("CRITICAL: A DID's payment") < message.body.index("WARNING")
    assert alerts.apply(db, findings[:1] + findings[2:], {"audit"}, NOW) is None  # events: once
    assert len(notifier.messages) == 1


@needs_db
def test_nothing_delivered_is_sent_again_next_time(engine: Engine) -> None:
    factory = session_factory(engine)
    with transaction(factory) as session:
        session.execute(text("DELETE FROM alert_states"))
    notifier = FakeNotifier(fail=True)
    alerts = dispatcher(notifier)
    finding = Finding("api.down", C, "The gateway API is not ready")
    with pytest.raises(DeliveryFailed), transaction(factory) as session:
        alerts.apply(session, [finding], {"api"}, NOW)
    notifier.fail = False
    with transaction(factory) as session:
        assert alerts.apply(session, [finding], {"api"}, NOW) is not None


# --------------------------------------------------------------------------- checks


class StubAdapter:
    def __init__(self, rail_id: str, recipient: str = MERCHANT, error: str | None = None) -> None:
        self.rail_id, self.recipient, self.error = rail_id, recipient, error

    def check_ready(self) -> None:
        if self.error:
            raise AdapterUnavailable(self.error)

    def configured_recipient(self) -> str:
        return self.recipient


class StubRpc:
    def __init__(self, height: int = 100, parent_desc: str = "") -> None:
        self.height, self.parent_desc = height, parent_desc

    def call(self, method: str, params: Any = None) -> Any:
        if method == "getblockchaininfo":
            return {"blocks": self.height}
        if method == "getaddressinfo":
            return {"ismine": True, "parent_desc": self.parent_desc}
        raise AssertionError(method)


def context(
    adapters: dict[str, Any] | None = None,
    *,
    api: Any = None,
    rpc: StubRpc | None = None,
    explorer_height: int = 100,
    environ: dict[str, str] | None = None,
    **settings: Any,
) -> mc.MonitorContext:
    environ = environ or {"DMD_USDC_BASE_INFERENCE_ADDRESS": INFERENCE}
    registry = load_registry(
        REGISTRY_PATH,
        environ=environ,
        available_adapters={"DMDNativePaymentAdapter", "X402PaymentAdapter"},
        available_price_sources={"dmd_mining_cost"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return api(request) if api else httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/api/getblockcount":
            return httpx.Response(200, text=str(explorer_height))
        if request.url.path == "/api/getdifficulty":
            return httpx.Response(200, text="1000")
        return httpx.Response(404)

    return mc.MonitorContext(
        Settings(_env_file=None, database_url=TEST_DATABASE_URL or UNREACHABLE_DB, **settings),
        environ,
        Runtime(registry, adapters or {}, {}),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
        (lambda: rpc) if rpc else None,
    )


@needs_db
def test_api_check(db: Session) -> None:
    assert mc.check_api(context(), db, NOW) == []
    down = context(
        api=lambda r: httpx.Response(503, json={"status": "unavailable", "checks": {"db": "x"}})
    )
    (finding,) = mc.check_api(down, db, NOW)
    assert (finding.key, finding.severity) == ("api.down", C)
    assert '{"db": "x"}' in finding.detail

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    (finding,) = mc.check_api(context(api=refused), db, NOW)
    assert finding.title == "The gateway API is not answering"


@needs_db
def test_worker_check(db: Session) -> None:
    ctx = context({"x402_usdc_base": StubAdapter("x402_usdc_base")})
    assert mc.check_api(ctx, db, NOW) == []
    (finding,) = mc.check_worker(ctx, db, NOW)
    assert finding.key == "worker.stopped" and "never" in finding.detail
    put_state(db, WORKER_HEARTBEAT, {"last_pass": {"errors": 0}}, NOW - timedelta(seconds=30))
    assert mc.check_worker(ctx, db, NOW) == []
    put_state(db, WORKER_HEARTBEAT, {"last_pass": {"errors": 2}}, NOW)
    (finding,) = mc.check_worker(ctx, db, NOW)
    assert (finding.key, finding.severity) == ("worker.errors", W)
    assert mc.check_worker(ctx, db, NOW + timedelta(minutes=3))[0].key == "worker.stopped"
    assert mc.check_worker(context(), db, NOW + timedelta(days=1)) == []  # nothing to run


@needs_db
def test_the_worker_records_its_heartbeat(engine: Engine) -> None:
    _heartbeat(session_factory(engine), NOW, {"processed": 1, "errors": 0})
    with session_factory(engine)() as session:
        state = get_state(session, WORKER_HEARTBEAT)
    assert state is not None and state[0] == {"last_pass": {"processed": 1, "errors": 0}}


@needs_db
def test_rails_check(db: Session) -> None:
    ok = StubAdapter("x402_usdc_base")
    assert mc.check_rails(context({"x402_usdc_base": ok}), db, NOW) == []
    broken = StubAdapter("x402_usdc_base", error="facilitator: down at https://x.test/k/SECRET")
    (finding,) = mc.check_rails(context({"x402_usdc_base": broken}), db, NOW)
    assert finding.key == "rails.x402_usdc_base" and finding.severity == C
    assert finding.title == "USDC payments (x402_usdc_base) cannot be verified"
    assert "DMD_RAIL_X402_USDC_BASE_KILL=true" in finding.detail


@needs_db
def test_price_check(db: Session) -> None:
    ctx = context(dmd_price_chain_data_from="explorer")
    (finding,) = mc.check_price(ctx, db, NOW)
    assert finding.key == "price.stale" and "ever" in finding.detail
    db.add(
        PriceRecord(
            source="dmd_mining_cost",
            asset_symbol="DMD",
            network="dmd:mainnet",
            usd_per_unit=Decimal("0.001"),
            observed_at=NOW - timedelta(minutes=2),
            inputs={"parameters_review_due_on": "2026-10-01"},
        )
    )
    db.flush()
    (finding,) = mc.check_price(ctx, db, NOW)
    assert finding.key == "price.review_due"
    assert [f.key for f in mc.check_price(ctx, db, NOW + timedelta(minutes=20))] == [
        "price.stale",
        "price.review_due",
    ]
    assert mc.check_price(context(), db, NOW) == []  # no price on this server


@needs_db
def test_dmd_chain_check(db: Session) -> None:
    environ = {
        "DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true",
        "DMD_USDC_BASE_INFERENCE_ADDRESS": INFERENCE,
    }
    adapters = {"dmd_pow_native": StubAdapter("dmd_pow_native")}
    near = context(adapters, rpc=StubRpc(height=101), explorer_height=100, environ=environ)
    assert mc.check_dmd_chain(near, db, NOW) == []
    far = context(adapters, rpc=StubRpc(height=90), explorer_height=100, environ=environ)
    (finding,) = mc.check_dmd_chain(far, db, NOW)
    assert finding.title == "The gateway's DMD node is 10 blocks behind the explorer"
    assert mc.check_dmd_chain(context(), db, NOW) == []  # no DMD rail


@needs_db
def test_audit_check(db: Session) -> None:
    ctx = context()
    record_audit_event(
        db, action="alert.old", subject_type="x", subject_id="old", actor_type="worker", data={}
    )
    db.flush()
    assert mc.check_audit(ctx, db, NOW) == []  # the first run starts from now
    intent = _intent(db, "dmd_payment_intent_refund", status="overpaid")
    record_audit_event(
        db,
        action="alert.issued_identity_payment_reorged",
        subject_type="identity",
        subject_id="dmd_identity_1",
        actor_type="worker",
        data={"payment_intent_id": intent.id},
    )
    record_audit_event(
        db,
        action="payment.refund_due",
        subject_type="payment_intent",
        subject_id=intent.id,
        actor_type="worker",
        data={"status": "overpaid", "refund_due_base_units": "1500000", "transactions": ["0xab"]},
    )
    record_audit_event(
        db, action="payment.settled", subject_type="x", subject_id="y", actor_type="worker"
    )
    db.flush()
    reorged, refund = mc.check_audit(ctx, db, NOW)
    assert (reorged.severity, reorged.kind) == (C, "event")
    assert reorged.title == "A DID's payment left the chain after the DID was issued"
    assert (refund.severity, refund.title) == (
        W,
        f"A payment needs a refund: 1.500000 USDC on {NETWORK}",
    )
    assert mc.check_audit(ctx, db, NOW) == []
    cursor = get_state(db, AUDIT_CURSOR)
    assert cursor is not None and cursor[0]["id"] >= 3


def _intent(db: Session, intent_id: str, *, status: str, **changes: Any) -> PaymentIntent:
    values: dict[str, Any] = {
        "id": intent_id,
        "rail_id": "x402_usdc_base",
        "adapter": "X402PaymentAdapter",
        "asset_symbol": "USDC",
        "asset_canonical_id": f"{NETWORK}/erc20:{TOKEN}",
        "asset_decimals": 6,
        "network": NETWORK,
        "amount_base_units": Decimal(25_000_000),
        "commercial_price_currency": "USD",
        "commercial_price_amount": Decimal("25.00"),
        "recipient": MERCHANT,
        "recipient_kind": "shared_with_reference",
        "payment_reference": intent_id,
        "required_confirmations": 5,
        "quote_expires_at": NOW + timedelta(minutes=15),
        "status": status,
        "created_at": NOW - timedelta(hours=3),
        "updated_at": NOW - timedelta(hours=3),
    }
    values.update(changes)
    # A payment intent belongs to a registration or a purchase; this one borrows a purchase.
    db.execute(
        text(
            "INSERT INTO purchases (id, product_id, commercial_price_currency, "
            "commercial_price_amount, terms_version, status) VALUES "
            "(:id, 'inference_donation', 'USD', 25.00, 't', 'awaiting_payment') "
            "ON CONFLICT DO NOTHING"
        ),
        {"id": f"p_{intent_id}"},
    )
    intent = PaymentIntent(purchase_id=f"p_{intent_id}", **values)
    db.add(intent)
    db.flush()
    return intent


@needs_db
def test_payments_check(db: Session) -> None:
    ctx = context()
    assert mc.check_payments(ctx, db, NOW) == []
    _intent(db, "dmd_payment_intent_slow", status="confirming")
    intent = _intent(db, "dmd_payment_intent_unknown", status="awaiting_payment")
    db.add(
        X402Authorization(
            payment_intent_id=intent.id,
            network=NETWORK,
            asset=TOKEN.lower(),
            pay_to=MERCHANT.lower(),
            payer="0x" + "aa" * 20,
            nonce="0x" + "bb" * 32,
            value=Decimal(25_000_000),
            valid_after=0,
            valid_before=4_000_000_000,
            payload={},
            requirements={},
            status="submitted",
            created_at=NOW - timedelta(hours=1),
            updated_at=NOW - timedelta(hours=1),
        )
    )
    db.flush()
    unknown, slow = mc.check_payments(ctx, db, NOW)
    assert unknown.key == "payments.x402_unknown" and "dmd_payment_intent_unknown" in unknown.detail
    assert slow.key == "payments.slow" and "dmd_payment_intent_slow" in slow.detail


@needs_db
def test_receiving_check(db: Session) -> None:
    descriptor = "wpkh([d34db33f/84h/0h/0h]xpub6CUGRUonZSQ4TWtTMmzXdrXDtyPWKiKbERr4/0/*)#abcd1234"
    rpc = StubRpc(parent_desc=descriptor)
    environ = {"DMD_USDC_BASE_INFERENCE_ADDRESS": INFERENCE}
    adapters = {"x402_usdc_base": StubAdapter("x402_usdc_base")}
    ctx = context(adapters, rpc=rpc, environ=environ)
    assert mc.check_receiving(ctx, db, NOW) == []  # the first run records what is in use
    assert mc.check_receiving(ctx, db, NOW) == []
    state = get_state(db, RECEIVING)
    assert state is not None and state[0]["x402_usdc_base"] == MERCHANT

    changed = context(
        {"x402_usdc_base": StubAdapter("x402_usdc_base", "0x" + "66" * 20)},
        rpc=rpc,
        environ=environ,
    )
    (finding,) = mc.check_receiving(changed, db, NOW)
    assert (finding.severity, finding.kind) == (C, "event")
    assert finding.title == "A receiving destination changed: x402_usdc_base"
    assert MERCHANT in finding.detail and "kill switch" in finding.detail

    # The DMD wallet is identified by its descriptor, never shown in full.
    shown = mc.describe_descriptor(descriptor)
    assert shown.startswith("wpkh [d34db33f/84h/0h/0h] key ") and "xpub" not in shown


@needs_db
def test_a_failing_check_is_reported_and_changes_nothing(db: Session) -> None:
    def broken(ctx: mc.MonitorContext, session: Session, now: datetime) -> list[Finding]:
        put_state(session, "half.done", {"x": 1}, now)
        raise RuntimeError("explorer at https://explorer.test/api?key=SECRET failed")

    findings, completed = mc.run_checks(context(), db, NOW, {"api": mc.check_api, "x": broken})
    (finding,) = findings
    assert finding.key == "monitor.x" and "SECRET" not in finding.detail
    assert completed == {"monitor", "api"}
    assert get_state(db, "half.done") is None


# --------------------------------------------------------------------------- reconciliation


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
    path = tmp_path_factory.mktemp("monitor") / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@needs_db
def test_daily_report_reconciles_with_the_chain(registry: Path, engine: Engine) -> None:
    from dmd_gateway.main import create_app

    from .fakes import fixed_price_factory

    reset_chain()
    app = create_app(
        Settings(
            _env_file=None,
            database_url=TEST_DATABASE_URL,
            route_registry_path=registry,
            x402_settlement_wait_seconds=0,
        ),
        environ=ENVIRON,
        engine=engine,
        adapters=ADAPTERS,
        price_sources={PRICE_SOURCE: fixed_price_factory(PRICE_SOURCE, "0.2")},
        issuer_key=ISSUER,
    )
    with TestClient(app) as client:
        q = quote(client)
        prove(client, q["registration_intent_id"])
        required = json.loads(
            __import__("base64").b64decode(
                register(client, q["registration_intent_id"]).headers["PAYMENT-REQUIRED"]
            )
        )
        assert (
            register(client, q["registration_intent_id"], TestPayer().header(required)).status_code
            == 201
        )
        donation_402 = client.post("/v1/donations/inference", json={"amount_usd": "2.00"})
        donation_required = json.loads(
            __import__("base64").b64decode(donation_402.headers["PAYMENT-REQUIRED"])
        )
        paid = client.post(
            "/v1/donations/inference",
            json={"amount_usd": "2.00"},
            headers={"PAYMENT-SIGNATURE": TestPayer().header(donation_required)},
        )
        assert paid.status_code == 201, paid.text

        state = app.state
        ctx = mc.MonitorContext(
            Settings(_env_file=None, database_url=TEST_DATABASE_URL),
            ENVIRON,
            Runtime(state.registry, state.quote_context.adapters, {}),
            httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        )
        now = datetime.now(UTC)
        factory = session_factory(engine)
        with transaction(factory) as session:
            report = build_report(ctx, session, now)
        body = "\n".join(report.lines)
        assert report.findings == []
        assert "- DIDs issued: 1" in body
        assert "- Inference donations completed: 1 (2.00 USD)" in body
        assert "- Payments settled with x402_usdc_base: 2, 27 USDC" in body
        assert f"- x402_usdc_base: {MERCHANT}" in body
        assert f"- donation.inference_donation.x402_usdc_base: {INFERENCE}" in body
        assert "checked again on chain, all found" in body
        message = report.message("staging")
        assert message.report and message.subject.endswith("all checks passed")
        assert message.subject.startswith("[DMD Gateway staging] Daily report")

        # The blocks holding both payments leave the chain.
        CHAIN.reorg()
        with transaction(factory) as session:
            report = build_report(ctx, session, now)
        (finding,) = report.findings
        assert finding.severity == C and finding.kind == "event"
        assert finding.title == "2 settled payment(s) no longer found on chain as credited"
        assert report.message("staging").subject.endswith("1 problem(s) to check")

        # A DID whose payment is no longer settled is reported as well.
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE payment_intents SET status = 'reorged' WHERE id = :p"),
                {"p": q["payment_intent_id"]},
            )
        with transaction(factory) as session:
            report = build_report(ctx, session, now)
        assert {f.title for f in report.findings} >= {"DIDs whose payment is not settled: 1"}
    reset_chain()


# --------------------------------------------------------------------------- the command


@needs_db
def test_check_command_sends_once_and_survives_a_lost_database(
    engine: Engine, tmp_path: Path
) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM alert_states"))
        conn.execute(text("DELETE FROM monitor_state"))
    notifier = FakeNotifier()
    alerts = dispatcher(notifier)
    environ = {
        **ENVIRON,
        "DMD_RAIL_X402_USDC_BASE_ENABLED": "false",
        "DMD_RAIL_DMD_POW_NATIVE_ENABLED": "false",
    }
    settings = Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        monitor_fallback_state_path=tmp_path / "fallback.json",
        dmd_price_chain_data_from="explorer",
    )
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ready"}))
    )
    assert run("check", settings, environ, alerts, http=http, now=NOW) == 0
    subjects = [m.subject for m in notifier.messages]
    assert subjects == ["[DMD Gateway staging] CRITICAL: 2 alerts"], notifier.messages[0].body
    assert run("check", settings, environ, alerts, http=http, now=NOW) == 0
    assert len(notifier.messages) == 1

    lost = settings.model_copy(update={"database_url": UNREACHABLE_DB})
    assert run("check", lost, environ, alerts, http=http, now=NOW) == 1
    assert notifier.messages[-1].subject.endswith("The gateway database is unreachable")
    assert run("check", lost, environ, alerts, http=http, now=NOW + timedelta(minutes=2)) == 1
    assert len(notifier.messages) == 2  # not again within the repeat delay
    assert run("check", settings, environ, alerts, http=http, now=NOW) == 0
    assert notifier.messages[-1].subject.endswith(
        "Resolved: The gateway database is reachable again"
    )


@needs_db
def test_report_command_sends_the_report(engine: Engine) -> None:
    notifier = FakeNotifier()
    environ = {
        **ENVIRON,
        "DMD_RAIL_X402_USDC_BASE_ENABLED": "false",
        "DMD_RAIL_DMD_POW_NATIVE_ENABLED": "false",
    }
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert run("report", settings, environ, dispatcher(notifier), http=http, now=NOW) == 0
    message = notifier.messages[-1]  # after any alert the report itself raised
    assert message.report and "Daily report 2026-10-27" in message.subject
    assert "RECEIVING DESTINATIONS IN USE" in message.body
    assert "OPEN ALERTS" in message.body

    notifier.fail = True
    assert run("report", settings, environ, dispatcher(notifier), http=http, now=NOW) == 1


def test_test_command_needs_a_channel(capsys: pytest.CaptureFixture[str]) -> None:
    from dmd_gateway.monitoring.cli import _test

    settings = Settings(_env_file=None)
    assert _test(settings, dispatcher()) == 1
    assert "No alert channel" in capsys.readouterr().out
    notifier = FakeNotifier()
    assert _test(settings, dispatcher(notifier)) == 0
    assert notifier.messages[0].subject == "[DMD Gateway development] Test message"
