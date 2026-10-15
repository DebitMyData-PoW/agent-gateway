import pytest

from dmd_gateway.domain.payment_status import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    PaymentStatus,
    assert_transition,
    can_transition,
    is_final,
)

S = PaymentStatus


def test_every_status_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(PaymentStatus)


def test_status_values_match_the_directive() -> None:
    assert [s.value for s in PaymentStatus] == [
        "created",
        "awaiting_payment",
        "detected",
        "confirming",
        "verified",
        "settled",
        "failed",
        "expired",
        "underpaid",
        "overpaid",
        "reorged",
        "reversed",
        "refunded",
        "disputed",
    ]


def test_happy_path() -> None:
    path = [S.CREATED, S.AWAITING_PAYMENT, S.DETECTED, S.CONFIRMING, S.VERIFIED, S.SETTLED]
    for current, new in zip(path, path[1:], strict=False):
        assert_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.AWAITING_PAYMENT, S.SETTLED),  # cannot skip verification
        (S.DETECTED, S.SETTLED),
        (S.CONFIRMING, S.SETTLED),
        (S.UNDERPAID, S.SETTLED),  # underpayment is never silently credited
        (S.EXPIRED, S.SETTLED),  # late payment is never silently credited
        (S.EXPIRED, S.DETECTED),
        (S.REFUNDED, S.SETTLED),
        (S.FAILED, S.SETTLED),
    ],
)
def test_forbidden_transitions(current: PaymentStatus, new: PaymentStatus) -> None:
    assert not can_transition(current, new)
    with pytest.raises(InvalidTransition):
        assert_transition(current, new)


def test_reorg_can_reopen_a_settled_payment() -> None:
    assert can_transition(S.SETTLED, S.REORGED)
    assert can_transition(S.REORGED, S.CONFIRMING)


def test_only_refunded_is_final() -> None:
    assert [s for s in PaymentStatus if is_final(s)] == [S.REFUNDED]


def test_settled_is_reachable_only_from_verified_or_disputed() -> None:
    sources = {s for s, targets in ALLOWED_TRANSITIONS.items() if S.SETTLED in targets}
    assert sources == {S.VERIFIED, S.DISPUTED}
