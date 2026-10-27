"""The daily reconciliation report (``dmd-gateway-monitor report``, DMD-13).

Once a day it answers three questions, by email and Discord:

1. What happened in the last 24 hours: quotes, DIDs issued, donations, payments settled per
   payment option (with the amounts received), payments that need a refund, and the
   receiving destinations in use.
2. Does the database agree with itself: every settled payment is fully credited, every DID
   and every donation receipt has a settled payment behind it.
3. Does the chain still agree with the database: every payment settled in the last 30 days
   is checked on its chain again, read-only, with the same adapters the worker uses. A
   payment no longer found with its confirmations is a critical alert.

The report holds counts, amounts and identifiers only: no names, keys or secrets.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from dmd_gateway.adapters.base import AdapterUnavailable
from dmd_gateway.db.models import (
    AgentIdentity,
    AlertState,
    AuditEvent,
    CreditedOutput,
    PaymentEvent,
    PaymentIntent,
    Purchase,
    RegistrationIntent,
)
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.monitoring.alerts import Finding, Message, Severity
from dmd_gateway.monitoring.checks import MonitorContext, receiving_destinations
from dmd_gateway.services.settlement import expected_payment

C, W = Severity.CRITICAL, Severity.WARNING
RECHECK_WINDOW = timedelta(days=30)
RECHECK_LIMIT = 500
SETTLED = "settled"


@dataclass
class Report:
    day: str
    lines: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def message(self, environment: str) -> Message:
        problems = [f for f in self.findings if f.severity == C]
        warnings = [f for f in self.findings if f.severity == W]
        if problems:
            verdict = f"{len(problems)} problem(s) to check"
        elif warnings:
            verdict = f"{len(warnings)} warning(s)"
        else:
            verdict = "all checks passed"
        return Message(
            subject=f"[DebitMyData Gateway {environment}] Daily report {self.day}: {verdict}",
            body="\n".join(self.lines).strip() + "\n",
            critical=bool(problems),
            report=True,
        )


def _amount(units: Decimal | int | None, decimals: int) -> str:
    return format_base_units(int(units or 0), decimals).rstrip("0").rstrip(".") or "0"


def build_report(ctx: MonitorContext, session: Session, now: datetime) -> Report:
    since = now - timedelta(days=1)
    report = Report(day=f"{now:%Y-%m-%d}")
    add = report.lines.append
    add(f"DebitMyData Agent Gateway ({ctx.settings.environment}), {now:%Y-%m-%d %H:%M} UTC")
    add("")

    # ------------------------------------------------------------------ last 24 hours
    add("LAST 24 HOURS")
    quotes = session.scalar(
        select(func.count())
        .select_from(RegistrationIntent)
        .where(RegistrationIntent.created_at >= since)
    )
    issued = session.scalar(
        select(func.count()).select_from(AgentIdentity).where(AgentIdentity.created_at >= since)
    )
    donations, donated = session.execute(
        select(func.count(), func.coalesce(func.sum(Purchase.commercial_price_amount), 0)).where(
            Purchase.status == "completed", Purchase.completed_at >= since
        )
    ).one()
    add(f"- DID registration quotes: {quotes}")
    add(f"- DIDs issued: {issued}")
    add(f"- Inference donations completed: {donations} ({Decimal(donated):.2f} USD)")
    settled = _settled_since(session, since)
    if settled:
        for (rail_id, symbol, decimals), (count, units) in sorted(settled.items()):
            add(f"- Payments settled with {rail_id}: {count}, {_amount(units, decimals)} {symbol}")
    else:
        add("- Payments settled: none")
    closed = session.scalar(
        select(func.count())
        .select_from(PaymentEvent)
        .where(
            PaymentEvent.recorded_at >= since,
            PaymentEvent.to_status.in_(("failed", "expired")),
            PaymentEvent.from_status.is_distinct_from(PaymentEvent.to_status),
        )
    )
    refunds = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "payment.refund_due", AuditEvent.occurred_at >= since)
    )
    add(f"- Payments failed or expired (nothing received): {closed}")
    add(f"- Payments that need a refund: {refunds}")
    add("")

    # ------------------------------------------------------------------ since the start
    add("SINCE THE START")
    total_ids = session.scalar(select(func.count()).select_from(AgentIdentity))
    add(f"- DIDs issued: {total_ids}")
    totals = _settled_since(session, None)
    for (rail_id, symbol, decimals), (count, units) in sorted(totals.items()):
        add(f"- Settled with {rail_id}: {count} payment(s), {_amount(units, decimals)} {symbol}")
    add("")

    # ------------------------------------------------------------------ destinations
    add("RECEIVING DESTINATIONS IN USE")
    try:
        destinations = receiving_destinations(ctx, session)
    except Exception as exc:  # noqa: BLE001 - shown in the report
        add(f"- could not be read: {type(exc).__name__}")
    else:
        for key, value in sorted(destinations.items()):
            add(f"- {key}: {value}")
        if not destinations:
            add("- none (no payment option is enabled)")
    add("Every change of these needs written approval.")
    add("")

    # ------------------------------------------------------------------ checks
    add("RECONCILIATION")
    report.findings += _database_checks(session, report.day, add)
    report.findings += _chain_checks(ctx, session, now, report.day, add)
    add("")

    open_alerts = session.scalars(
        select(AlertState)
        .where(AlertState.resolved_at.is_(None), AlertState.kind == "condition")
        .order_by(AlertState.first_seen_at)
    ).all()
    add("OPEN ALERTS")
    for alert in open_alerts:
        add(
            f"- {alert.severity.upper()}: {alert.title} "
            f"(since {alert.first_seen_at:%Y-%m-%d %H:%M} UTC)"
        )
    if not open_alerts:
        add("- none")
    for finding in report.findings:
        add("")
        add(f"{finding.severity.value.upper()}: {finding.title}")
        add(finding.detail)
    return report


def _settled_since(
    session: Session, since: datetime | None
) -> dict[tuple[str, str, int], tuple[int, Decimal]]:
    """(rail, asset, decimals) -> (payments, base units credited) for payments that reached
    settled (in the period, or ever)."""
    query = (
        select(
            PaymentIntent.rail_id,
            PaymentIntent.asset_symbol,
            PaymentIntent.asset_decimals,
            func.count(func.distinct(PaymentIntent.id)),
        )
        .join(PaymentEvent, PaymentEvent.payment_intent_id == PaymentIntent.id)
        .where(PaymentEvent.to_status == SETTLED, PaymentEvent.from_status != SETTLED)
        .group_by(PaymentIntent.rail_id, PaymentIntent.asset_symbol, PaymentIntent.asset_decimals)
    )
    if since is not None:
        query = query.where(PaymentEvent.recorded_at >= since)
    counts = {(r, s, d): n for r, s, d, n in session.execute(query)}
    credited = select(
        PaymentIntent.rail_id,
        PaymentIntent.asset_symbol,
        PaymentIntent.asset_decimals,
        func.coalesce(func.sum(CreditedOutput.amount_base_units), 0),
    ).join(CreditedOutput, CreditedOutput.payment_intent_id == PaymentIntent.id)
    if since is not None:
        credited = credited.where(CreditedOutput.credited_at >= since)
    credited = credited.group_by(
        PaymentIntent.rail_id, PaymentIntent.asset_symbol, PaymentIntent.asset_decimals
    )
    units: dict[tuple[str, str, int], Decimal] = defaultdict(Decimal)
    for rail_id, symbol, decimals, total in session.execute(credited):
        units[(rail_id, symbol, decimals)] = Decimal(total)
    return {key: (counts.get(key, 0), units.get(key, Decimal(0))) for key in counts | units}


def _database_checks(session: Session, day: str, add: Callable[[str], None]) -> list[Finding]:
    findings = []
    credited_sum = (
        select(func.coalesce(func.sum(CreditedOutput.amount_base_units), 0))
        .where(CreditedOutput.payment_intent_id == PaymentIntent.id)
        .scalar_subquery()
    )
    short = session.scalars(
        select(PaymentIntent.id).where(
            PaymentIntent.status == SETTLED, credited_sum < PaymentIntent.amount_base_units
        )
    ).all()
    settled_payment = exists().where(PaymentIntent.status == SETTLED)
    unpaid_dids = session.scalars(
        select(AgentIdentity.id).where(
            ~settled_payment.where(
                PaymentIntent.registration_intent_id == AgentIdentity.registration_intent_id
            )
        )
    ).all()
    unpaid_receipts = session.scalars(
        select(Purchase.id).where(
            Purchase.status == "completed",
            ~settled_payment.where(PaymentIntent.purchase_id == Purchase.id),
        )
    ).all()
    for name, ids, title in (
        ("short", short, "Settled payments not fully credited"),
        ("unpaid_dids", unpaid_dids, "DIDs whose payment is not settled"),
        ("unpaid_receipts", unpaid_receipts, "Donation receipts whose payment is not settled"),
    ):
        if ids:
            findings.append(
                Finding(
                    f"reconcile.{name}.{day}",
                    C,
                    f"{title}: {len(ids)}",
                    "IDs: " + ", ".join(ids[:10]) + (" …" if len(ids) > 10 else ""),
                    kind="event",
                )
            )
    if not findings:
        add(
            "- Database: every settled payment is fully credited; every DID and receipt has a "
            "settled payment."
        )
    return findings


def _chain_checks(
    ctx: MonitorContext, session: Session, now: datetime, day: str, add: Callable[[str], None]
) -> list[Finding]:
    intents = session.scalars(
        select(PaymentIntent)
        .where(
            PaymentIntent.status == SETTLED,
            PaymentIntent.updated_at >= now - RECHECK_WINDOW,
        )
        .order_by(PaymentIntent.updated_at.desc())
        .limit(RECHECK_LIMIT)
    ).all()
    checked = unavailable = skipped = 0
    missing: list[str] = []
    for intent in intents:
        adapter = ctx.runtime.adapters.get(intent.rail_id)
        if adapter is None:
            skipped += 1  # that payment option is switched off on this server now
            continue
        credited = session.scalars(
            select(CreditedOutput).where(CreditedOutput.payment_intent_id == intent.id)
        ).all()
        try:
            observation = adapter.observe(expected_payment(session, intent))
        except AdapterUnavailable:
            unavailable += 1
            continue
        checked += 1
        seen = {
            (output.transaction_reference, output.output_index): output.confirmations
            for output in observation.outputs
        }
        required = intent.required_confirmations or 1
        if not credited or any(
            seen.get((c.transaction_reference, c.output_index), -1) < required for c in credited
        ):
            missing.append(intent.id)
    add(
        f"- Chain: {checked} payment(s) settled in the last 30 days checked again on chain"
        + (f", {len(missing)} not found as credited" if missing else ", all found")
        + (f"; {unavailable} could not be checked (chain unreachable)" if unavailable else "")
        + (f"; {skipped} on payment options now switched off" if skipped else "")
        + "."
    )
    findings = []
    if missing:
        findings.append(
            Finding(
                f"reconcile.chain.{day}",
                C,
                f"{len(missing)} settled payment(s) no longer found on chain as credited",
                "Payments: "
                + ", ".join(missing[:10])
                + (" …" if len(missing) > 10 else "")
                + "\nThe DIDs or receipts they paid for may have been given without payment. "
                "Check each one with GET /v1/payments/{id} and on the chain's explorer.",
                kind="event",
            )
        )
    if unavailable:
        findings.append(
            Finding(
                f"reconcile.unavailable.{day}",
                W,
                f"{unavailable} payment(s) could not be checked again on chain",
                "The chain or its node could not be reached during the daily check.",
                kind="event",
            )
        )
    return findings
