"""Which payments the worker checks in each pass: open ones first, then every watched one in
turn, and settled DMD payments watched for reorganizations for 7 days."""

# ruff: noqa: F811  (pytest fixtures imported from test_worker are used as parameters)

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from dmd_gateway.domain.routes import ConfirmationPolicy, load_registry
from dmd_gateway.services import settlement

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL
from .fakes import SCRIPTED
from .test_quotes import ready_registry  # noqa: F401
from .test_worker import DUE, clean_chain, client, engine, quote, scalar, status, tick  # noqa: F401

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def small_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passes of 10 intents with 3 kept for watched ones, so a few quotes fill a pass."""
    monkeypatch.setattr(settlement, "BATCH_SIZE", 10)
    monkeypatch.setattr(settlement, "WATCHED_RESERVE", 3)


def set_age(db: Engine, payment_ids: list[str], age: timedelta) -> None:
    """Pretend these intents last changed status `age` ago."""
    with db.begin() as conn:
        conn.execute(
            text("UPDATE payment_intents SET updated_at = now() - :age WHERE id = ANY(:ids)"),
            {"age": age, "ids": payment_ids},
        )


def expire_all(app_client, db: Engine, payment_ids: list[str], at: datetime) -> None:
    for _ in range(10):
        tick(app_client, at)
        if not scalar(
            db,
            "SELECT count(*) FROM payment_intents WHERE id = ANY(:ids) AND status <> 'expired'",
            ids=payment_ids,
        ):
            return
    raise AssertionError("quotes did not expire")


def settle(app_client) -> str:
    pid = quote(app_client)["payment_intent_id"]
    SCRIPTED.pay(pid, DUE, txid=f"s{pid[-8:]}", confirmations=6)
    tick(app_client)
    assert status(app_client, pid)["status"] == "settled"
    return pid


def test_an_open_payment_is_checked_even_behind_many_closed_quotes(
    small_batches,
    client,
    engine,
) -> None:
    # More expired quotes than fit in a pass, all older than the new quote. Before 0.15.0
    # the worker took the oldest first, so the new payment waited until they aged out.
    closed = [quote(client)["payment_intent_id"] for _ in range(15)]
    expire_all(client, engine, closed, datetime.now(UTC) + timedelta(minutes=16))
    set_age(engine, closed, timedelta(hours=1))

    pid = quote(client)["payment_intent_id"]
    SCRIPTED.pay(pid, DUE, txid="open1")
    tick(client)
    assert status(client, pid)["status"] == "confirming"


def test_every_watched_payment_is_checked_in_turn(small_batches, client, engine) -> None:
    watched = [quote(client)["payment_intent_id"] for _ in range(25)]
    start = datetime.now(UTC) + timedelta(minutes=20)
    expire_all(client, engine, watched, start - timedelta(minutes=4))

    due = scalar(
        engine,
        "SELECT count(*) FROM payment_intents WHERE status IN ('expired', 'settled', 'underpaid')"
        " AND updated_at > :since",
        since=start - timedelta(hours=24),
    )
    for i in range(math.ceil(due / 10) + 1):
        tick(client, start + timedelta(seconds=i))
    never_rechecked = scalar(
        engine,
        "SELECT count(*) FROM payment_intents WHERE id = ANY(:ids)"
        " AND (last_checked_at IS NULL OR last_checked_at < :start)",
        ids=watched,
        start=start,
    )
    assert never_rechecked == 0


def test_watched_payments_keep_places_during_a_flood_of_open_quotes(
    small_batches,
    client,
    engine,
) -> None:
    pid = settle(client)
    for _ in range(15):  # more open quotes than a whole pass
        quote(client)
    SCRIPTED.confirm(pid, 0)  # the settled payment's block was disconnected
    tick(client)
    assert status(client, pid)["status"] == "confirming"  # the reorg was still noticed


def test_settled_dmd_payments_are_watched_for_seven_days(client, engine) -> None:
    three_days, eight_days = settle(client), settle(client)
    set_age(engine, [three_days], timedelta(days=3))
    set_age(engine, [eight_days], timedelta(days=8))
    SCRIPTED.confirm(three_days, 0)
    SCRIPTED.confirm(eight_days, 0)
    tick(client)
    assert status(client, three_days)["status"] == "confirming"  # watched: reorg noticed
    assert status(client, eight_days)["status"] == "settled"  # past 7 days: left to reconciliation


def test_expired_quotes_are_still_watched_for_24_hours_only(client, engine) -> None:
    pid = quote(client)["payment_intent_id"]
    expired_at = datetime.now(UTC) + timedelta(minutes=16)
    expire_all(client, engine, [pid], expired_at)
    set_age(engine, [pid], timedelta(days=2))
    SCRIPTED.pay(pid, DUE, txid="late2", first_seen_at=expired_at + timedelta(hours=1))
    tick(client)
    assert status(client, pid)["refund_due"] is None  # not watched any more


def test_a_payment_that_cannot_be_checked_goes_to_the_back_of_the_queue(
    client,
    engine,
) -> None:
    pid = quote(client)["payment_intent_id"]
    SCRIPTED.unavailable = True
    now = datetime.now(UTC)
    result = tick(client, now)
    assert result.unavailable >= 1
    checked = scalar(engine, "SELECT last_checked_at FROM payment_intents WHERE id = :p", p=pid)
    assert checked == now
    assert status(client, pid)["status"] == "awaiting_payment"  # nothing else changed


def test_shipped_dmd_rail_watches_settled_payments_for_seven_days() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    policy = registry.rails["dmd_pow_native"].confirmation_policy
    assert policy.watch_after_settlement_hours == 168
    assert registry.rails["x402_usdc_base"].confirmation_policy.watch_after_settlement_hours is None


@pytest.mark.parametrize("hours", [0, 721])
def test_watch_after_settlement_hours_is_bounded(hours: int) -> None:
    with pytest.raises(ValidationError):
        ConfirmationPolicy(required_confirmations=6, watch_after_settlement_hours=hours)


def test_workers_side_by_side_each_check_their_own_share(
    monkeypatch: pytest.MonkeyPatch, client, engine
) -> None:
    """Several payment workers: every open payment belongs to exactly one of them, so none is
    checked twice at once and none is left out."""
    monkeypatch.setattr(settlement, "BATCH_SIZE", 100_000)  # every due payment in one pass
    pids = {quote(client)["payment_intent_id"] for _ in range(8)}
    rails = list(client.app.state.quote_context.adapters)
    now = datetime.now(UTC)
    with Session(engine) as session:
        shares = [
            set(settlement.due_intent_ids(session, rails, now, shard=settlement.Shard(i, 3)))
            for i in range(3)
        ]
        everything = set(settlement.due_intent_ids(session, rails, now))
    assert pids <= everything
    for one, other in [(0, 1), (0, 2), (1, 2)]:
        assert not shares[one] & shares[other]
    assert set().union(*shares) == everything
    with pytest.raises(ValueError, match="0 <= index < count"):
        settlement.Shard(3, 3)


def test_a_worker_never_checks_a_payment_again_too_soon(
    monkeypatch: pytest.MonkeyPatch, client, engine
) -> None:
    """With ``rest``, a payment checked less than that long ago waits: a worker going on
    without a pause (a backlog) never checks the same payment more often than before."""
    monkeypatch.setattr(settlement, "BATCH_SIZE", 100_000)  # every due payment in one pass
    pid = quote(client)["payment_intent_id"]
    rails = list(client.app.state.quote_context.adapters)
    now = datetime.now(UTC)
    tick(client, now)  # checked now
    rest = timedelta(seconds=10)
    with Session(engine) as session:
        soon = settlement.due_intent_ids(session, rails, now + timedelta(seconds=5), rest=rest)
        later = settlement.due_intent_ids(session, rails, now + timedelta(seconds=11), rest=rest)
    assert pid not in soon and pid in later
    result = tick(client, now + timedelta(seconds=1))
    assert result.due >= 1  # without rest, as before: every pass takes it


def test_rails_of_other_companies_keep_their_pace() -> None:
    """A rail with checks_per_second is checked at most that often, shared between the
    workers; a rail without one (the gateway's own node) never waits."""

    class Rail:
        def __init__(self, rate: float | None) -> None:
            self.checks_per_second = rate

    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(round(seconds, 6))
        clock[0] += seconds

    pacer = settlement.RailPacer(
        {"card": Rail(20.0), "dmd": Rail(None)},  # type: ignore[dict-item]
        shards=4,
        clock=lambda: clock[0],
        sleep=sleep,
    )
    for _ in range(3):
        pacer.wait("dmd")
    assert slept == []
    for _ in range(3):
        pacer.wait("card")  # 20 a second for 4 workers: 5 a second each, 0.2 s apart
    assert slept == [0.2, 0.2]
