"""Applies payment observations to the database (GW-11, DMD-06..DMD-11, DMD-13).

For each open payment intent the worker:
  1. locks the row (``FOR UPDATE SKIP LOCKED``, so several workers never double-process);
  2. asks the rail's adapter what it independently sees on chain;
  3. applies the status changes decided by ``domain.settlement`` and records each one in
     the append-only ``payment_events`` and ``audit_events`` tables;
  4. on settlement, credits each paying output exactly once in ``credited_outputs`` and
     marks the registration (or the purchase) as paid;
  5. records any amount that must be refunded (over-, under- or late payment).

If the node is unreachable nothing changes: a payment is never expired, failed or settled
without a successful observation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import not_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.adapters.base import (
    AdapterUnavailable,
    ExpectedPayment,
    PaymentAdapter,
    PaymentDestination,
    SubmittedAuthorization,
)
from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import (
    CreditedOutput,
    PaymentEvent,
    PaymentIntent,
    Purchase,
    RegistrationIntent,
    X402Authorization,
)
from dmd_gateway.db.session import transaction
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.domain.settlement import (
    ACTIVE_STATUSES,
    WATCHED_AFTER_COMPLETION,
    SettlementContext,
    plan,
    refund_due,
    summarize,
)
from dmd_gateway.services.payment_references import submitted_references

log = logging.getLogger(__name__)
S = PaymentStatus

# How long settled / expired / underpaid intents keep being watched for reorgs and late or
# extra payments before only periodic reconciliation looks at them. A rail can watch its
# settled payments longer (``confirmation_policy.watch_after_settlement_hours``).
WATCH_WINDOW = timedelta(hours=24)
BATCH_SIZE = 100
# Places in each pass kept for watched intents, so a flood of open quotes cannot stop
# the worker from noticing a reorganized payment.
WATCHED_RESERVE = 20


class DoubleCreditError(RuntimeError):
    """A payment output is already credited to a different intent (DMD-11)."""


@dataclass(frozen=True)
class PassResult:
    processed: int = 0
    changed: int = 0
    unavailable: int = 0
    errors: int = 0


def settled_watch_windows(adapters: Mapping[str, PaymentAdapter]) -> dict[str, timedelta]:
    """Rails that watch their settled payments for longer than ``WATCH_WINDOW``."""
    windows: dict[str, timedelta] = {}
    for rail_id, adapter in adapters.items():
        hours = adapter.rail.confirmation_policy.watch_after_settlement_hours
        if hours and timedelta(hours=hours) > WATCH_WINDOW:
            windows[rail_id] = timedelta(hours=hours)
    return windows


def watched_recheck_intervals(adapters: Mapping[str, PaymentAdapter]) -> dict[str, timedelta]:
    """Rails whose closed payments are checked again only every so often (card payments:
    each check is a request to the payment provider)."""
    intervals: dict[str, timedelta] = {}
    for rail_id, adapter in adapters.items():
        minutes = adapter.rail.confirmation_policy.recheck_after_settlement_minutes
        if minutes:
            intervals[rail_id] = timedelta(minutes=minutes)
    return intervals


def due_intent_ids(
    session: Session,
    rail_ids: list[str],
    now: datetime,
    settled_windows: Mapping[str, timedelta] | None = None,
    recheck_intervals: Mapping[str, timedelta] | None = None,
) -> list[str]:
    """The intents to check in this pass, at most ``BATCH_SIZE``.

    Open payments (someone is waiting for them) come first, then closed ones still watched
    for reorganizations and late payments, which always keep ``WATCHED_RESERVE`` places when
    there are enough of them. In each group the least recently checked come first, so every
    intent is checked in turn, however many there are. Closed payments of a rail with a
    recheck interval wait that long between two checks.
    """
    if not rail_ids:
        return []
    order = (
        PaymentIntent.last_checked_at.asc().nulls_first(),
        PaymentIntent.updated_at,
        PaymentIntent.id,
    )
    open_ids = list(
        session.scalars(
            select(PaymentIntent.id)
            .where(PaymentIntent.rail_id.in_(rail_ids))
            .where(PaymentIntent.status.in_([s.value for s in ACTIVE_STATUSES]))
            .order_by(*order)
            .limit(BATCH_SIZE)
        )
    )
    watched = [
        PaymentIntent.status.in_([s.value for s in WATCHED_AFTER_COMPLETION])
        & (PaymentIntent.updated_at > now - WATCH_WINDOW)
    ]
    for rail_id, window in (settled_windows or {}).items():
        if rail_id in rail_ids:
            watched.append(
                (PaymentIntent.rail_id == rail_id)
                & (PaymentIntent.status == S.SETTLED.value)
                & (PaymentIntent.updated_at > now - window)
            )
    query = select(PaymentIntent.id).where(PaymentIntent.rail_id.in_(rail_ids)).where(or_(*watched))
    checked_lately = [
        (PaymentIntent.rail_id == rail_id)
        & PaymentIntent.last_checked_at.is_not(None)
        & (PaymentIntent.last_checked_at > now - every)
        for rail_id, every in (recheck_intervals or {}).items()
        if rail_id in rail_ids
    ]
    if checked_lately:
        query = query.where(not_(or_(*checked_lately)))
    watched_ids = list(
        session.scalars(
            query.order_by(*order).limit(max(BATCH_SIZE - len(open_ids), WATCHED_RESERVE))
        )
    )
    return open_ids[: BATCH_SIZE - len(watched_ids)] + watched_ids


def _mark_checked(factory: sessionmaker[Session], payment_intent_id: str, now: datetime) -> None:
    """Send an intent that could not be checked to the back of the queue, so it cannot
    hold up the others (its rail is down, or checking it fails)."""
    try:
        with transaction(factory) as session:
            session.execute(
                update(PaymentIntent)
                .where(PaymentIntent.id == payment_intent_id)
                .values(last_checked_at=now)
            )
    except Exception:
        log.exception(
            "could not record a payment check",
            extra={"event": {"payment_intent_id": payment_intent_id}},
        )


def _last_event(session: Session, payment_intent_id: str) -> PaymentEvent | None:
    return session.scalars(
        select(PaymentEvent)
        .where(PaymentEvent.payment_intent_id == payment_intent_id)
        .order_by(PaymentEvent.id.desc())
        .limit(1)
    ).first()


def _known_transactions(session: Session, payment_intent_id: str) -> tuple[str, ...]:
    """Transactions seen before plus references submitted by the payer (hints only)."""
    seen: dict[str, None] = {}
    for detail in session.scalars(
        select(PaymentEvent.detail).where(PaymentEvent.payment_intent_id == payment_intent_id)
    ):
        for txid in (detail or {}).get("transactions", []):
            seen[txid] = None
    for reference in submitted_references(session, payment_intent_id):
        seen[reference] = None
    return tuple(seen)


# Authorizations that reached the facilitator's settlement step. One refused at
# verification never went further, so no funds can have moved for it.
SUBMITTED_STATUSES = ("submitted", "settled", "settle_rejected")


def submitted_authorizations(
    session: Session, payment_intent_id: str
) -> tuple[SubmittedAuthorization, ...]:
    """x402 authorizations sent for settlement for this intent, oldest first."""
    rows = session.scalars(
        select(X402Authorization)
        .where(
            X402Authorization.payment_intent_id == payment_intent_id,
            X402Authorization.status.in_(SUBMITTED_STATUSES),
        )
        .order_by(X402Authorization.id)
    )
    return tuple(
        SubmittedAuthorization(
            payer=row.payer,
            nonce=row.nonce,
            submitted_at=row.created_at,
            transaction_hint=row.settle_transaction,
            search_from_block=row.search_from_block,
        )
        for row in rows
    )


def _detail(
    reason: str, transactions: list[str], refund_units: int, extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "reason": reason,
        "transactions": transactions,
        "refund_due_base_units": str(refund_units),
    }
    if extra:
        detail.update(extra)
    return detail


def expected_payment(session: Session, intent: PaymentIntent) -> ExpectedPayment:
    """What the adapter should look for on chain for this intent."""
    return ExpectedPayment(
        payment_intent_id=intent.id,
        network=intent.network,
        asset_canonical_id=intent.asset_canonical_id,
        amount_base_units=int(intent.amount_base_units),
        destination=PaymentDestination(
            recipient=intent.recipient,
            recipient_kind=intent.recipient_kind,
            payment_reference=intent.payment_reference,
        ),
        required_confirmations=intent.required_confirmations,
        known_transactions=_known_transactions(session, intent.id),
        authorizations=submitted_authorizations(session, intent.id),
    )


def process_intent(
    session: Session,
    payment_intent_id: str,
    adapters: Mapping[str, PaymentAdapter],
    now: datetime,
) -> bool:
    """Observe one intent and record what changed. Returns True if anything was recorded."""
    intent = session.scalars(
        select(PaymentIntent)
        .where(PaymentIntent.id == payment_intent_id)
        .with_for_update(skip_locked=True)
    ).first()
    if intent is None:
        return False
    adapter = adapters.get(intent.rail_id)
    if adapter is None:
        return False
    intent.last_checked_at = now  # saved with the rest, or by run_pass if this check fails

    current = S(intent.status)
    expected_units = int(intent.amount_base_units)
    expected = expected_payment(session, intent)
    observation = adapter.observe(expected)  # AdapterUnavailable propagates: no change
    ctx = SettlementContext(
        expected_base_units=expected_units,
        required_confirmations=intent.required_confirmations or 1,
        quote_expires_at=intent.quote_expires_at,
        now=now,
    )
    summary = summarize(observation, ctx)
    steps = plan(current, observation, ctx)
    final = steps[-1].to_status if steps else current
    refund_units = refund_due(final, summary, ctx)
    transactions = list(observation.transaction_references)
    first_output = summary.timely[0] if summary.timely else None
    common = {
        "transaction_reference": first_output.transaction_reference if first_output else None,
        "observed_amount_base_units": Decimal(summary.received_base_units),
        "confirmations": summary.confirmations,
        "block_hash": first_output.block_hash if first_output else None,
        "block_height": first_output.block_height if first_output else None,
        "observed_at": observation.observed_at,
    }

    last = _last_event(session, intent.id)
    previous_refund = int((last.detail or {}).get("refund_due_base_units", 0)) if last else 0

    def capped(confirmations: int | None) -> int | None:
        # Confirmations past the requirement are not recorded one by one.
        return None if confirmations is None else min(confirmations, ctx.required_confirmations)

    progressed = last is None or (
        capped(last.confirmations) != capped(summary.confirmations)
        or int(last.observed_amount_base_units or 0) != summary.received_base_units
        or sorted((last.detail or {}).get("transactions", [])) != sorted(transactions)
        or previous_refund != refund_units
    )
    if not steps and not progressed:
        return False

    for step in steps:
        session.add(
            PaymentEvent(
                payment_intent_id=intent.id,
                from_status=step.from_status.value,
                to_status=step.to_status.value,
                detail=_detail(step.reason, transactions, refund_units),
                **common,
            )
        )
        record_audit_event(
            session,
            action="payment.status_changed",
            subject_type="payment_intent",
            subject_id=intent.id,
            actor_type="worker",
            actor_id=adapter.name,
            data={
                "from": step.from_status.value,
                "to": step.to_status.value,
                "reason": step.reason,
                "transactions": transactions,
                "received_base_units": str(summary.received_base_units),
                "confirmations": summary.confirmations,
            },
        )
    if not steps:
        session.add(
            PaymentEvent(
                payment_intent_id=intent.id,
                from_status=current.value,
                to_status=current.value,
                detail=_detail("progress", transactions, refund_units),
                **common,
            )
        )

    if refund_units != previous_refund and refund_units > 0:
        record_audit_event(
            session,
            action="payment.refund_due",
            subject_type="payment_intent",
            subject_id=intent.id,
            actor_type="worker",
            actor_id=adapter.name,
            data={
                "status": final.value,
                "refund_due_base_units": str(refund_units),
                "late_base_units": str(summary.late_base_units),
                "transactions": transactions,
            },
        )

    reached = {step.to_status for step in steps}
    if S.SETTLED in reached and final is S.SETTLED:
        _credit_outputs(session, intent, summary.timely)
    if intent.purchase_id is not None:
        _sync_purchase(session, intent, current, final, reached, now)
    else:
        _sync_registration(session, intent, current, final, reached, now)

    if final is not current:
        intent.status = final.value
    intent.updated_at = now if steps else intent.updated_at
    return True


def _credit_outputs(session: Session, intent: PaymentIntent, outputs: tuple) -> None:
    for output in outputs:
        key = (intent.network, output.transaction_reference, output.output_index)
        existing = session.get(CreditedOutput, key)
        if existing is None:
            session.add(
                CreditedOutput(
                    network=intent.network,
                    transaction_reference=output.transaction_reference,
                    output_index=output.output_index,
                    payment_intent_id=intent.id,
                    amount_base_units=Decimal(output.amount_base_units),
                )
            )
        elif existing.payment_intent_id != intent.id:
            raise DoubleCreditError(
                f"{output.transaction_reference}:{output.output_index} is already credited "
                f"to {existing.payment_intent_id}"
            )
    record_audit_event(
        session,
        action="payment.settled",
        subject_type="payment_intent",
        subject_id=intent.id,
        actor_type="worker",
        data={
            "outputs": [f"{o.transaction_reference}:{o.output_index}" for o in outputs],
            "amount_base_units": str(sum(o.amount_base_units for o in outputs)),
        },
    )


# Payment statuses where the money went back to the payer after the payment was settled.
TAKEN_BACK = frozenset({S.REFUNDED, S.DISPUTED, S.REVERSED})


def _sync_registration(
    session: Session,
    intent: PaymentIntent,
    current: PaymentStatus,
    final: PaymentStatus,
    reached: set[PaymentStatus],
    now: datetime,
) -> None:
    registration = session.get(RegistrationIntent, intent.registration_intent_id)
    if registration is None:
        return
    new_status = None
    if final is S.SETTLED and registration.status == "awaiting_payment":
        new_status = "paid"
    elif final is S.EXPIRED and registration.status == "awaiting_payment":
        new_status = "expired"
    elif final in TAKEN_BACK and reached & TAKEN_BACK:
        # A card payment refunded or disputed: no DID-LLM is issued for it any more, and one
        # already issued is for a person to look at (it is not revoked automatically: a
        # dispute can still be won).
        if registration.status in ("awaiting_payment", "paid"):
            new_status = "cancelled"
        elif registration.status == "issued":
            record_audit_event(
                session,
                action="alert.issued_identity_payment_taken_back",
                subject_type="registration_intent",
                subject_id=registration.id,
                actor_type="worker",
                data={"payment_intent_id": intent.id, "payment_status": final.value},
            )
    elif current is S.SETTLED and S.REORGED in reached:
        if registration.status == "paid":
            new_status = "awaiting_payment"
        elif registration.status == "issued":
            record_audit_event(
                session,
                action="alert.issued_identity_payment_reorged",
                subject_type="registration_intent",
                subject_id=registration.id,
                actor_type="worker",
                data={"payment_intent_id": intent.id},
            )
    if new_status:
        registration.status = new_status
        registration.updated_at = now


def _sync_purchase(
    session: Session,
    intent: PaymentIntent,
    current: PaymentStatus,
    final: PaymentStatus,
    reached: set[PaymentStatus],
    now: datetime,
) -> None:
    """The purchase follows its payment: paid once settled, failed if nothing arrived."""
    purchase = session.get(Purchase, intent.purchase_id, with_for_update=True)
    if purchase is None:
        return
    new_status = None
    if final is S.SETTLED and purchase.status == "awaiting_payment":
        new_status = "paid"
    elif final in (S.FAILED, S.EXPIRED) and purchase.status == "awaiting_payment":
        new_status = "failed"
    elif current is S.SETTLED and S.REORGED in reached:
        if purchase.status == "paid":
            new_status = "awaiting_payment"
        elif purchase.status == "completed":
            record_audit_event(
                session,
                action="alert.completed_purchase_payment_reorged",
                subject_type="purchase",
                subject_id=purchase.id,
                actor_type="worker",
                data={"payment_intent_id": intent.id},
            )
    if new_status:
        record_audit_event(
            session,
            action="purchase.status_changed",
            subject_type="purchase",
            subject_id=purchase.id,
            actor_type="worker",
            data={"from": purchase.status, "to": new_status, "payment_intent_id": intent.id},
        )
        purchase.status = new_status
        purchase.updated_at = now


def run_pass(
    factory: sessionmaker[Session],
    adapters: Mapping[str, PaymentAdapter],
    now: datetime,
) -> PassResult:
    with transaction(factory) as session:
        ids = due_intent_ids(
            session,
            list(adapters),
            now,
            settled_watch_windows(adapters),
            watched_recheck_intervals(adapters),
        )
    processed = changed = unavailable = errors = 0
    for payment_intent_id in ids:
        try:
            with transaction(factory) as session:
                if process_intent(session, payment_intent_id, adapters, now):
                    changed += 1
            processed += 1
        except AdapterUnavailable as exc:
            unavailable += 1
            log.warning(
                "payment rail unavailable",
                extra={"event": {"payment_intent_id": payment_intent_id, "error": str(exc)}},
            )
            _mark_checked(factory, payment_intent_id, now)
        except Exception:
            errors += 1
            log.exception(
                "payment processing failed",
                extra={"event": {"payment_intent_id": payment_intent_id}},
            )
            _mark_checked(factory, payment_intent_id, now)
    return PassResult(processed, changed, unavailable, errors)
