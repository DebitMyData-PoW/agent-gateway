"""Normalized payment status shared by every payment adapter.

Every adapter (native DMD, x402, EVM, Solana, ...) must report one of these states, and
every change of state must follow ``ALLOWED_TRANSITIONS``. The DID service only looks at
these normalized states, never at chain-specific details.
"""

from __future__ import annotations

from enum import StrEnum


class PaymentStatus(StrEnum):
    CREATED = "created"
    AWAITING_PAYMENT = "awaiting_payment"
    DETECTED = "detected"
    CONFIRMING = "confirming"
    VERIFIED = "verified"
    SETTLED = "settled"
    FAILED = "failed"
    EXPIRED = "expired"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    REORGED = "reorged"
    REVERSED = "reversed"
    REFUNDED = "refunded"
    DISPUTED = "disputed"


S = PaymentStatus

# Underpaid / overpaid / late-payment handling follows the open refund policy decision
# (DEC-04). Until it is decided these paths only lead to refund or failure, never to a
# silent credit.
ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    S.CREATED: frozenset({S.AWAITING_PAYMENT, S.EXPIRED, S.FAILED}),
    S.AWAITING_PAYMENT: frozenset({S.DETECTED, S.EXPIRED, S.FAILED}),
    S.DETECTED: frozenset({S.CONFIRMING, S.UNDERPAID, S.OVERPAID, S.REORGED, S.FAILED}),
    S.CONFIRMING: frozenset({S.VERIFIED, S.REORGED, S.FAILED}),
    S.VERIFIED: frozenset({S.SETTLED, S.REORGED, S.FAILED}),
    S.SETTLED: frozenset({S.REORGED, S.REVERSED, S.REFUNDED, S.DISPUTED}),
    S.UNDERPAID: frozenset({S.REFUNDED, S.FAILED}),
    S.OVERPAID: frozenset({S.CONFIRMING, S.REFUNDED, S.FAILED}),
    S.REORGED: frozenset({S.AWAITING_PAYMENT, S.DETECTED, S.CONFIRMING, S.FAILED}),
    S.REVERSED: frozenset({S.REFUNDED, S.DISPUTED}),
    S.DISPUTED: frozenset({S.SETTLED, S.REVERSED, S.REFUNDED}),
    # A payment that arrives after its quote expired is returned, never credited.
    S.EXPIRED: frozenset({S.REFUNDED}),
    # Funds that reached a failed intent can still be returned.
    S.FAILED: frozenset({S.REFUNDED}),
    S.REFUNDED: frozenset(),
}


class InvalidTransition(ValueError):
    def __init__(self, current: PaymentStatus, new: PaymentStatus) -> None:
        super().__init__(f"payment status cannot move from {current} to {new}")
        self.current = current
        self.new = new


def can_transition(current: PaymentStatus, new: PaymentStatus) -> bool:
    return new in ALLOWED_TRANSITIONS[current]


def assert_transition(current: PaymentStatus, new: PaymentStatus) -> None:
    if not can_transition(current, new):
        raise InvalidTransition(current, new)


def is_final(status: PaymentStatus) -> bool:
    return not ALLOWED_TRANSITIONS[status]
