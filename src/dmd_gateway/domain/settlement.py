"""Rail-independent settlement rules: observation -> normalized payment status.

Every adapter reports raw facts (outputs, confirmations, conflicts). These rules decide the
status, so all rails share one lifecycle (directive: "every adapter must return the same
normalized payment status").

    awaiting_payment -> detected -> confirming -> verified -> settled
                           |  \\-> overpaid -> confirming
                           \\-> underpaid (quote expired before the full amount arrived)
    awaiting_payment -> expired (no payment before the quote expired)
    any confirmed state -> reorged -> re-evaluated; double-spent -> failed

Payment policy (DEC-04, chosen from the directive's "never silently credit" rule):
  * Only outputs first seen before the quote expired count towards the payment.
  * Underpayment: the payer may top up until the quote expires; then ``underpaid`` and the
    received amount is due for refund. Nothing is credited.
  * Overpayment: accepted, and the excess is recorded as due for refund.
  * Late payment (after expiry): never credited; recorded as due for refund.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dmd_gateway.adapters.base import ObservedOutput, PaymentObservation
from dmd_gateway.domain.payment_status import PaymentStatus, assert_transition

S = PaymentStatus

# Statuses the worker keeps checking on every pass.
ACTIVE_STATUSES = frozenset(
    {S.AWAITING_PAYMENT, S.DETECTED, S.CONFIRMING, S.VERIFIED, S.OVERPAID, S.REORGED}
)
# Statuses the worker keeps watching for a while, for reorgs and late or extra payments.
WATCHED_AFTER_COMPLETION = frozenset({S.SETTLED, S.EXPIRED, S.UNDERPAID})

MAX_STEPS = 10


@dataclass(frozen=True, slots=True)
class SettlementContext:
    expected_base_units: int
    required_confirmations: int
    quote_expires_at: datetime
    now: datetime


@dataclass(frozen=True, slots=True)
class Summary:
    timely: tuple[ObservedOutput, ...]
    late: tuple[ObservedOutput, ...]
    received_base_units: int
    late_base_units: int
    confirmations: int | None
    conflicted: bool
    expired: bool

    @property
    def has_payment(self) -> bool:
        return bool(self.timely)


@dataclass(frozen=True, slots=True)
class Step:
    from_status: PaymentStatus
    to_status: PaymentStatus
    reason: str


def summarize(observation: PaymentObservation, ctx: SettlementContext) -> Summary:
    timely = tuple(o for o in observation.outputs if o.first_seen_at <= ctx.quote_expires_at)
    late = tuple(o for o in observation.outputs if o.first_seen_at > ctx.quote_expires_at)
    return Summary(
        timely=timely,
        late=late,
        received_base_units=sum(o.amount_base_units for o in timely),
        late_base_units=sum(o.amount_base_units for o in late),
        confirmations=min((o.confirmations for o in timely), default=None),
        conflicted=bool(observation.conflicted_transactions),
        expired=ctx.now > ctx.quote_expires_at,
    )


def _gone(s: Summary) -> tuple[PaymentStatus, str]:
    if s.conflicted:
        return S.FAILED, "payment transaction was double-spent or replaced"
    return S.REORGED, "payment is no longer in the chain or mempool"


def _next(
    status: PaymentStatus, s: Summary, ctx: SettlementContext
) -> tuple[PaymentStatus, str] | None:
    expected = ctx.expected_base_units
    if status is S.CREATED:
        return S.AWAITING_PAYMENT, "quote issued"

    if status is S.AWAITING_PAYMENT:
        if s.has_payment:
            return S.DETECTED, "payment detected"
        if s.expired:
            return S.EXPIRED, "quote expired before payment arrived"
        return None

    if status is S.DETECTED:
        if not s.has_payment:
            return _gone(s)
        if s.received_base_units > expected:
            return S.OVERPAID, "more than the quoted amount was received"
        if s.received_base_units == expected:
            return S.CONFIRMING, "full amount received"
        if s.expired:
            return S.UNDERPAID, "quote expired before the full amount arrived"
        return None  # partial payment; the payer may still top up before expiry

    if status is S.OVERPAID:
        if not s.has_payment:
            return S.FAILED, "payment is no longer visible"
        return S.CONFIRMING, "overpayment accepted; the excess is due for refund"

    if status is S.CONFIRMING:
        if not s.has_payment or s.received_base_units < expected:
            return _gone(s)
        if s.confirmations and s.confirmations >= 1:
            return S.VERIFIED, "payment confirmed in a block and independently verified"
        return None

    if status is S.VERIFIED:
        if not s.has_payment or s.received_base_units < expected:
            return _gone(s)
        if not s.confirmations:
            return S.REORGED, "the confirming block left the active chain"
        if s.confirmations >= ctx.required_confirmations:
            return S.SETTLED, f"{s.confirmations} confirmations reached"
        return None

    if status is S.SETTLED:
        if not s.has_payment or s.received_base_units < expected or not s.confirmations:
            return S.REORGED, "settled payment was affected by a chain reorganization"
        return None

    if status is S.REORGED:
        if s.has_payment:
            return S.DETECTED, "payment seen again after reorganization"
        if s.conflicted:
            return S.FAILED, "payment transaction was double-spent"
        return S.AWAITING_PAYMENT, "waiting for payment again after reorganization"

    return None  # expired, underpaid, failed, refunded, reversed, disputed: operator handles


def plan(
    current: PaymentStatus, observation: PaymentObservation, ctx: SettlementContext
) -> list[Step]:
    """All status changes implied by one observation, in order (possibly none)."""
    summary = summarize(observation, ctx)
    steps: list[Step] = []
    status = current
    for _ in range(MAX_STEPS):
        decision = _next(status, summary, ctx)
        if decision is None:
            break
        new_status, reason = decision
        assert_transition(status, new_status)
        steps.append(Step(status, new_status, reason))
        status = new_status
    return steps


def refund_due(status: PaymentStatus, s: Summary, ctx: SettlementContext) -> int:
    """Base units received that must be returned, given the resulting status."""
    if status in (S.UNDERPAID, S.EXPIRED, S.FAILED):
        return s.received_base_units + s.late_base_units
    if status in (S.CONFIRMING, S.VERIFIED, S.SETTLED, S.OVERPAID):
        return max(0, s.received_base_units - ctx.expected_base_units) + s.late_base_units
    return s.late_base_units
