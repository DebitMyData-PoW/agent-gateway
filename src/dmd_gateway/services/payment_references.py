"""Payer-submitted transaction references (DMD-12; directive tool dmd_submit_payment_reference).

After broadcasting a payment, a payer may tell the gateway its transaction reference. The
reference is only a hint for where to look: the payment adapter still verifies the asset,
network, destination, amount and confirmations itself, so a wrong or foreign reference
can never credit anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import PaymentIntent, PaymentReference
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.domain.settlement import ACTIVE_STATUSES
from dmd_gateway.errors import ApiError

MAX_REFERENCES = 5
X402_ADAPTER = "X402PaymentAdapter"

_FORMATS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("dmd:", re.compile(r"^[0-9a-f]{64}$"), "a DMD transaction ID (64 hexadecimal characters)"),
    ("eip155:", re.compile(r"^0x[0-9a-f]{64}$"), "an EVM transaction hash (0x + 64 hex)"),
)
_GENERIC = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")


def normalize_reference(network: str, reference: str) -> str:
    value = reference.strip()
    for prefix, pattern, wording in _FORMATS:
        if network.startswith(prefix):
            value = value.lower()
            if not pattern.match(value):
                raise ApiError(422, "invalid_reference", f"Expected {wording}.")
            return value
    if not _GENERIC.match(value):
        raise ApiError(422, "invalid_reference", "Not a valid transaction reference.")
    return value


@dataclass(frozen=True)
class SubmittedReference:
    payment_intent_id: str
    reference: str
    created: bool


def submit_reference(
    session: Session, payment_intent_id: str, reference: str, now: datetime
) -> SubmittedReference:
    intent = session.get(PaymentIntent, payment_intent_id, with_for_update=True)
    if intent is None:
        raise ApiError(404, "not_found", "Unknown payment intent.")
    if intent.adapter == X402_ADAPTER:
        # x402 payments go through POST /v1/dids/registrations; with one shared receiving
        # address, a reference from a payer could point at someone else's payment.
        raise ApiError(
            409,
            "not_used_for_x402",
            "x402 payments are made and tracked through POST /v1/dids/registrations; "
            "transaction references are not used for them.",
        )
    value = normalize_reference(intent.network, reference)

    existing = session.scalars(
        select(PaymentReference).where(
            PaymentReference.payment_intent_id == intent.id,
            PaymentReference.reference == value,
        )
    ).first()
    if existing is not None:
        return SubmittedReference(intent.id, value, created=False)

    if PaymentStatus(intent.status) not in ACTIVE_STATUSES:
        raise ApiError(
            409,
            "payment_not_open",
            f"This payment is {intent.status}; references are only taken for open payments.",
        )
    count = session.scalar(
        select(func.count())
        .select_from(PaymentReference)
        .where(PaymentReference.payment_intent_id == intent.id)
    )
    if count >= MAX_REFERENCES:
        raise ApiError(
            429,
            "too_many_references",
            f"At most {MAX_REFERENCES} references can be submitted for one payment.",
        )

    session.add(PaymentReference(payment_intent_id=intent.id, reference=value, submitted_at=now))
    record_audit_event(
        session,
        action="payment.reference_submitted",
        subject_type="payment_intent",
        subject_id=intent.id,
        actor_type="agent",
        data={"reference": value},
    )
    return SubmittedReference(intent.id, value, created=True)


def submitted_references(session: Session, payment_intent_id: str) -> list[str]:
    return list(
        session.scalars(
            select(PaymentReference.reference)
            .where(PaymentReference.payment_intent_id == payment_intent_id)
            .order_by(PaymentReference.id)
        )
    )
