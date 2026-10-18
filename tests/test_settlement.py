"""Settlement rules shared by every payment rail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dmd_gateway.adapters.base import ObservedOutput, PaymentObservation
from dmd_gateway.domain.payment_status import PaymentStatus as S
from dmd_gateway.domain.settlement import SettlementContext, plan, refund_due, summarize

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
EXPIRES = NOW + timedelta(minutes=10)
DUE = 12_500_000_000  # 125 DMD


def ctx(now: datetime = NOW) -> SettlementContext:
    return SettlementContext(
        expected_base_units=DUE, required_confirmations=6, quote_expires_at=EXPIRES, now=now
    )


def out(units: int = DUE, conf: int = 0, seen: datetime = NOW, txid: str = "aa") -> ObservedOutput:
    return ObservedOutput(
        txid, 0, units, conf, seen, "bb" * 32 if conf else None, 7 if conf else None
    )


def obs(*outputs: ObservedOutput, conflicted: tuple[str, ...] = ()) -> PaymentObservation:
    return PaymentObservation(
        "pi", "fake", NOW, outputs=outputs, conflicted_transactions=conflicted
    )


def path(current: S, observation: PaymentObservation, context: SettlementContext | None = None):
    return [step.to_status for step in plan(current, observation, context or ctx())]


def test_nothing_happens_without_payment() -> None:
    assert path(S.AWAITING_PAYMENT, obs()) == []


def test_exact_payment_in_mempool_goes_to_confirming() -> None:
    assert path(S.AWAITING_PAYMENT, obs(out())) == [S.DETECTED, S.CONFIRMING]


def test_one_confirmation_verifies() -> None:
    assert path(S.CONFIRMING, obs(out(conf=1))) == [S.VERIFIED]


def test_required_confirmations_settle() -> None:
    assert path(S.VERIFIED, obs(out(conf=5))) == []
    assert path(S.VERIFIED, obs(out(conf=6))) == [S.SETTLED]


def test_deeply_confirmed_payment_found_late_walks_every_step() -> None:
    assert path(S.AWAITING_PAYMENT, obs(out(conf=10))) == [
        S.DETECTED,
        S.CONFIRMING,
        S.VERIFIED,
        S.SETTLED,
    ]


def test_partial_payment_waits_for_top_up_until_expiry() -> None:
    partial = obs(out(units=DUE - 1))
    assert path(S.AWAITING_PAYMENT, partial) == [S.DETECTED]
    assert path(S.DETECTED, partial) == []
    after_expiry = ctx(EXPIRES + timedelta(seconds=1))
    assert path(S.DETECTED, partial, after_expiry) == [S.UNDERPAID]


def test_top_up_completes_the_payment() -> None:
    two_parts = obs(out(units=DUE - 100, txid="a1"), out(units=100, txid="a2"))
    assert path(S.DETECTED, two_parts) == [S.CONFIRMING]


def test_overpayment_is_accepted_and_excess_refunded() -> None:
    observation = obs(out(units=DUE + 500))
    assert path(S.AWAITING_PAYMENT, observation) == [S.DETECTED, S.OVERPAID, S.CONFIRMING]
    summary = summarize(observation, ctx())
    assert refund_due(S.CONFIRMING, summary, ctx()) == 500


def test_no_payment_expires() -> None:
    assert path(S.AWAITING_PAYMENT, obs(), ctx(EXPIRES + timedelta(seconds=1))) == [S.EXPIRED]


def test_late_payment_is_never_credited() -> None:
    late = obs(out(seen=EXPIRES + timedelta(seconds=5)))
    context = ctx(EXPIRES + timedelta(minutes=1))
    assert path(S.AWAITING_PAYMENT, late, context) == [S.EXPIRED]
    assert refund_due(S.EXPIRED, summarize(late, context), context) == DUE


def test_payment_seen_before_expiry_still_confirms_after_expiry() -> None:
    timely = obs(out(conf=6, seen=EXPIRES - timedelta(minutes=1)))
    assert path(S.AWAITING_PAYMENT, timely, ctx(EXPIRES + timedelta(hours=1)))[-1] is S.SETTLED


def test_underpayment_is_refunded() -> None:
    partial = obs(out(units=1_000))
    context = ctx(EXPIRES + timedelta(seconds=1))
    assert refund_due(S.UNDERPAID, summarize(partial, context), context) == 1_000


def test_double_spend_before_confirmation_fails() -> None:
    assert path(S.CONFIRMING, obs(conflicted=("aa",))) == [S.FAILED]


def test_reorg_of_verified_payment_back_to_mempool() -> None:
    # Block disconnected: the transaction is back in the mempool with 0 confirmations.
    assert path(S.VERIFIED, obs(out(conf=0))) == [S.REORGED, S.DETECTED, S.CONFIRMING]


def test_reorg_of_settled_payment_is_reopened() -> None:
    assert path(S.SETTLED, obs(out(conf=0))) == [S.REORGED, S.DETECTED, S.CONFIRMING]


def test_settled_payment_double_spent_by_reorg_fails() -> None:
    assert path(S.SETTLED, obs(conflicted=("aa",))) == [S.REORGED, S.FAILED]


def test_settled_payment_stays_settled() -> None:
    assert path(S.SETTLED, obs(out(conf=50))) == []


def test_terminal_statuses_are_left_to_operators() -> None:
    for status in (S.EXPIRED, S.UNDERPAID, S.FAILED, S.REFUNDED):
        assert path(status, obs(out(conf=10))) == []
