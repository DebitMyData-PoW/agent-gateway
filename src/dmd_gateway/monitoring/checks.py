"""Health checks run by ``dmd-gateway-monitor check`` (every 2 minutes on the servers).

Each check looks at one thing and returns what is wrong, as findings (``alerts.Finding``):

  api        the API answers its readiness check
  worker     the payment worker is alive (it records a heartbeat on every pass)
  rails      every enabled payment option can still verify payments (node, chain RPC,
             facilitator), with the same checks the gateway runs at start
  price      the DMD price is fresh, and its parameters are not past their review
  dmd_chain  the gateway's DebitMyData mainnet node agrees with the public explorer on the height
  audit      alerts recorded by the worker (a payment reorganized out of the chain after its
             DID or receipt was issued) and payments that need a refund
  payments   USDC settlements with an unknown outcome, and payments confirming for too long
  receiving  the receiving addresses in use did not change since the last run; a change that
             was not approved in writing is the first sign of a compromised server
  backups    the nightly encrypted backup succeeded within the last 26 hours (production)

A check that cannot run is reported as such, and the alerts it raised earlier stay open.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dmd_gateway.adapters.base import AdapterUnavailable
from dmd_gateway.adapters.dmd_rpc import DmdRpcClient
from dmd_gateway.db.models import AuditEvent, PaymentIntent, X402Authorization
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.monitoring.alerts import Finding, Severity, redact
from dmd_gateway.monitoring.state import (
    AUDIT_CURSOR,
    RECEIVING,
    WORKER_HEARTBEAT,
    get_state,
    put_state,
)
from dmd_gateway.pricing.mining_cost import DmdExplorerFigures, latest_record
from dmd_gateway.runtime import Runtime
from dmd_gateway.settings import Settings

C, W = Severity.CRITICAL, Severity.WARNING
DMD_RAIL = "dmd_pow_native"
DEFAULT_PRICE_AGE = timedelta(seconds=900)
X402_UNKNOWN_AFTER = timedelta(minutes=30)
CONFIRMING_TOO_LONG = timedelta(hours=2)
HEIGHT_TOLERANCE = 3  # blocks between the node and the explorer
AUDIT_BATCH = 200
SHOWN_IDS = 5
CONFIRMING = (PaymentStatus.DETECTED, PaymentStatus.CONFIRMING, PaymentStatus.VERIFIED)
AUDIT_TITLES = {
    "alert.issued_identity_payment_reorged": (
        "A DID's payment left the chain after the DID was issued"
    ),
    "alert.completed_purchase_payment_reorged": (
        "A donation's payment left the chain after its receipt was issued"
    ),
}


@dataclass
class MonitorContext:
    settings: Settings
    environ: Mapping[str, str]
    runtime: Runtime
    http: httpx.Client
    dmd_rpc: Callable[[], Any] | None = None  # returns a client with .call(method, params)

    def rpc(self) -> Any:
        return self.dmd_rpc() if self.dmd_rpc else DmdRpcClient.from_environ(self.environ)


Check = Callable[[MonitorContext, Session, datetime], list[Finding]]


# --------------------------------------------------------------------------- the checks


def check_api(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    url = ctx.settings.monitor_api_url
    try:
        response = ctx.http.get(url, timeout=10)
        body = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
    except (httpx.HTTPError, ValueError) as exc:
        return [Finding("api.down", C, "The gateway API is not answering", type(exc).__name__)]
    if response.status_code == 200 and body.get("status") == "ready":
        return []
    checks = json.dumps(body.get("checks", {}), sort_keys=True)
    return [
        Finding(
            "api.down",
            C,
            "The gateway API is not ready",
            f"Readiness check: HTTP {response.status_code}, {checks}. Agents cannot get "
            "quotes or register while this lasts.",
        )
    ]


def worker_expected(ctx: MonitorContext) -> bool:
    return bool(ctx.runtime.adapters) or ctx.settings.dmd_price_chain_data_from != "off"


def check_worker(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    if not worker_expected(ctx):
        return []
    limit = max(timedelta(minutes=2), timedelta(seconds=6 * ctx.settings.worker_interval_seconds))
    state = get_state(session, WORKER_HEARTBEAT)
    if state is None or now - state[1] > limit:
        seen = f"{state[1]:%Y-%m-%d %H:%M} UTC" if state else "never"
        return [
            Finding(
                "worker.stopped",
                C,
                "The payment worker has stopped",
                f"Last sign of life: {seen}. Payments are not being verified, so DIDs and "
                "receipts are not issued after payment, and the DMD price is not updated. "
                "On the server: systemctl status dmd-gateway-worker",
            )
        ]
    errors = int((state[0].get("last_pass") or {}).get("errors", 0))
    if errors:
        return [
            Finding(
                "worker.errors",
                W,
                "The payment worker fails on some payments",
                f"{errors} payment(s) failed in its last pass; see the worker's log "
                "(journalctl -u dmd-gateway-worker).",
            )
        ]
    return []


def check_rails(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    findings = []
    for rail_id, adapter in ctx.runtime.adapters.items():
        rail = ctx.runtime.registry.rails[rail_id]
        try:
            adapter.check_ready()
        except AdapterUnavailable as exc:
            findings.append(
                Finding(
                    f"rails.{rail_id}",
                    C,
                    f"{rail.asset.symbol} payments ({rail_id}) cannot be verified",
                    f"{exc}\nPayments with this option are not confirmed until this is fixed. "
                    f"To stop new quotes for it, set its kill switch ({rail.kill_env}=true) "
                    "and restart the gateway.",
                )
            )
    return findings


def check_price(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    if ctx.settings.dmd_price_chain_data_from == "off":
        return []
    rail = ctx.runtime.registry.rails.get(DMD_RAIL)
    max_age = DEFAULT_PRICE_AGE
    if rail is not None and rail.price_policy.max_price_age_seconds:
        max_age = timedelta(seconds=rail.price_policy.max_price_age_seconds)
    record = latest_record(session)
    findings = []
    if record is None or now - record.observed_at > max_age:
        age = (
            f"{int((now - record.observed_at).total_seconds() // 60)} minutes" if record else "ever"
        )
        findings.append(
            Finding(
                "price.stale",
                W,
                "The DMD price is not being updated",
                f"Last update: {age} ago. DMD quotes are refused until it is updated again "
                "(the payment worker computes it every 5 minutes from the DebitMyData chain).",
            )
        )
    due = (record.inputs or {}).get("parameters_review_due_on") if record else None
    if due and now.date() > date.fromisoformat(str(due)):
        findings.append(
            Finding(
                "price.review_due",
                W,
                "The DMD price parameters are due for their quarterly review",
                f"Review due on {due}: the electricity price and machine efficiency in "
                "config/payment_routes.yaml (dmd_mining_cost).",
            )
        )
    return findings


def check_dmd_chain(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    rail = ctx.runtime.registry.rails.get(DMD_RAIL)
    if DMD_RAIL not in ctx.runtime.adapters or rail is None or rail.network != "dmd:mainnet":
        return []
    node = int(ctx.rpc().call("getblockchaininfo")["blocks"])
    explorer = DmdExplorerFigures(ctx.http, ctx.settings.dmd_explorer_url).read().height
    if abs(node - explorer) <= HEIGHT_TOLERANCE:
        return []
    behind = "behind" if node < explorer else "ahead of"
    return [
        Finding(
            "dmd_chain.height",
            W,
            f"The gateway's DebitMyData node is {abs(node - explorer)} blocks {behind} the "
            "explorer",
            f"Node: block {node}. Public explorer: block {explorer}. A node behind sees DMD "
            "payments late; a large difference can also mean the node or the explorer is on "
            "another chain.",
        )
    ]


def check_audit(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    cursor = get_state(session, AUDIT_CURSOR)
    if cursor is None:
        # First run: start from now, without sending the history.
        last = session.scalar(select(func.max(AuditEvent.id))) or 0
        put_state(session, AUDIT_CURSOR, {"id": int(last)}, now)
        return []
    after = int(cursor[0].get("id", 0))
    events = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.id > after,
            AuditEvent.action.like("alert.%") | (AuditEvent.action == "payment.refund_due"),
        )
        .order_by(AuditEvent.id)
        .limit(AUDIT_BATCH)
    ).all()
    findings = []
    for event in events:
        if event.action == "payment.refund_due":
            findings.append(_refund_finding(session, event))
        else:
            data = json.dumps(event.data, sort_keys=True)
            when = event.occurred_at.strftime("%Y-%m-%d %H:%M")
            findings.append(
                Finding(
                    f"audit.{event.id}",
                    C,
                    AUDIT_TITLES.get(event.action, f"Alert recorded: {event.action}"),
                    f"{event.subject_type} {event.subject_id}, {when} UTC.\n{data}\nThe "
                    "payment is watched again; if it does not come back on chain, the DID or "
                    "receipt was given without payment.",
                    kind="event",
                )
            )
    if events:
        newest = events[-1].id
    else:
        newest = session.scalar(select(func.max(AuditEvent.id))) or after
    put_state(session, AUDIT_CURSOR, {"id": int(max(newest, after))}, now)
    return findings


def _refund_finding(session: Session, event: AuditEvent) -> Finding:
    intent = session.get(PaymentIntent, event.subject_id)
    units = int(event.data.get("refund_due_base_units", 0))
    amount = (
        f"{format_base_units(units, intent.asset_decimals)} {intent.asset_symbol} on "
        f"{intent.network}"
        if intent is not None
        else f"{units} base units"
    )
    transactions = ", ".join(event.data.get("transactions", [])) or "-"
    return Finding(
        f"audit.{event.id}",
        W,
        f"A payment needs a refund: {amount}",
        f"Payment {event.subject_id}, status {event.data.get('status')}. Transactions: "
        f"{transactions}. See the refund policy (/policies/refunds) for how refunds are made.",
        kind="event",
    )


def check_payments(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    findings = []
    unknown = session.scalars(
        select(X402Authorization.payment_intent_id).where(
            X402Authorization.status == "submitted",
            X402Authorization.updated_at < now - X402_UNKNOWN_AFTER,
        )
    ).all()
    if unknown:
        findings.append(
            Finding(
                "payments.x402_unknown",
                W,
                f"{len(unknown)} USDC payment(s) with an unknown settlement outcome",
                "Sent to the facilitator more than 30 minutes ago and not found on chain yet; "
                "the worker keeps looking. Payments: " + _ids(unknown),
            )
        )
    slow = session.scalars(
        select(PaymentIntent.id).where(
            PaymentIntent.status.in_([s.value for s in CONFIRMING]),
            PaymentIntent.updated_at < now - CONFIRMING_TOO_LONG,
        )
    ).all()
    if slow:
        findings.append(
            Finding(
                "payments.slow",
                W,
                f"{len(slow)} payment(s) confirming for more than 2 hours",
                "Seen on chain but not yet confirmed enough. Payments: " + _ids(slow),
            )
        )
    return findings


def _ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:SHOWN_IDS])
    return shown + (f" and {len(ids) - SHOWN_IDS} more" if len(ids) > SHOWN_IDS else "")


# --------------------------------------------------------------------------- receiving


_ORIGIN = re.compile(r"\[([0-9a-fA-F]{8}(?:/[0-9]+['h]?)*)\]")


def describe_descriptor(descriptor: str) -> str:
    """A DMD wallet descriptor shown without its public key (which would reveal every
    address of the wallet): its script, key origin and a short fingerprint."""
    body = descriptor.split("#", 1)[0]
    script = body.split("(", 1)[0]
    origin = _ORIGIN.search(body)
    digest = hashlib.sha256(body.encode()).hexdigest()[:12]
    return f"{script} [{origin.group(1) if origin else 'no origin'}] key {digest}"


def receiving_destinations(ctx: MonitorContext, session: Session) -> dict[str, str]:
    registry = ctx.runtime.registry
    current: dict[str, str] = {}
    for rail_id, adapter in ctx.runtime.adapters.items():
        rail = registry.rails[rail_id]
        if rail.recipient.source == "env":
            current[f"{rail_id}"] = adapter.configured_recipient()
        elif rail.recipient.source == "per_intent_address" and rail.protocol == "dmd_native":
            address = session.scalar(
                select(PaymentIntent.recipient)
                .where(PaymentIntent.rail_id == rail_id)
                .order_by(PaymentIntent.created_at.desc())
                .limit(1)
            )
            if address:
                info = ctx.rpc().call("getaddressinfo", {"address": address})
                parent = str(info.get("parent_desc") or "")
                if not info.get("ismine") or not parent:
                    raise RuntimeError(
                        f"the latest {rail_id} payment address is not from the gateway's wallet"
                    )
                current[f"{rail_id}"] = describe_descriptor(parent)
    for donation_id in registry.donations:
        for rail_id, _rail, address in registry.donation_rails(donation_id):
            current[f"donation.{donation_id}.{rail_id}"] = address
    return current


def check_receiving(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    current = receiving_destinations(ctx, session)
    previous = get_state(session, RECEIVING)
    put_state(session, RECEIVING, current, now)
    if previous is None:
        return []
    before = previous[0]
    findings = []
    for key, value in sorted(current.items()):
        old = before.get(key)
        if old == value:
            continue
        digest = hashlib.sha256(f"{key}={value}".encode()).hexdigest()[:12]
        if old is None:
            findings.append(
                Finding(
                    f"receiving.{key}.{digest}",
                    W,
                    f"A new receiving destination is in use: {key}",
                    f"Now: {value}",
                    kind="event",
                )
            )
        else:
            findings.append(
                Finding(
                    f"receiving.{key}.{digest}",
                    C,
                    f"A receiving destination changed: {key}",
                    f"Before: {old}\nNow: {value}\nEvery change needs written approval. If "
                    "this one was not approved, switch this payment option off with its kill "
                    "switch at once and check who changed the server's configuration.",
                    kind="event",
                )
            )
    return findings


BACKUP_MAX_AGE = timedelta(hours=26)


def check_backups(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    path = ctx.settings.backup_status_path
    if path is None:
        return []
    try:
        last: datetime | None = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        last = None
    if last is not None and last.tzinfo is not None and now - last <= BACKUP_MAX_AGE:
        return []
    when = (
        f"{int((now - last).total_seconds() // 3600)} hours ago"
        if last is not None and last.tzinfo is not None
        else "never"
    )
    return [
        Finding(
            "backups.stale",
            W,
            "The nightly backup has not succeeded",
            f"Last successful backup: {when}. On the server: systemctl status "
            "dmd-gateway-backup; journalctl -u dmd-gateway-backup -n 50 (docs/production.md).",
        )
    ]


CHECKS: dict[str, Check] = {
    "api": check_api,
    "worker": check_worker,
    "rails": check_rails,
    "price": check_price,
    "dmd_chain": check_dmd_chain,
    "audit": check_audit,
    "payments": check_payments,
    "receiving": check_receiving,
    "backups": check_backups,
}


def run_checks(
    ctx: MonitorContext,
    session: Session,
    now: datetime | None = None,
    checks: Mapping[str, Check] = CHECKS,
) -> tuple[list[Finding], set[str]]:
    """Every check, each in its own savepoint: one that fails changes nothing and is
    reported, without resolving the alerts it raised before."""
    now = now or datetime.now(UTC)
    findings: list[Finding] = []
    completed = {"monitor"}
    for name, check in checks.items():
        try:
            with session.begin_nested():
                found = check(ctx, session, now)
        except Exception as exc:  # noqa: BLE001 - any failure is reported, not raised
            findings.append(
                Finding(
                    f"monitor.{name}",
                    W,
                    f"The monitor could not run its {name} check",
                    f"{type(exc).__name__}: {redact(str(exc))[:300]}",
                )
            )
            continue
        findings.extend(found)
        completed.add(name)
    return findings, completed
