"""Health checks run by ``dmd-gateway-monitor check`` (every 2 minutes on the servers).

Each check looks at one thing and returns what is wrong, as findings (``alerts.Finding``):

  api        the API answers its readiness check
  worker     the payment worker is alive (it records a heartbeat on every pass)
  rails      every enabled payment option can still verify payments (node, chain RPC,
             facilitator), with the same checks the gateway runs at start; a provider that
             only refuses because of its rate limit (HTTP 429) usually recovers by itself, so
             that alerts only after RATE_LIMIT_ALERT_AFTER checks in a row
  price      the DMD price is fresh, and its parameters are not past their review
  dmd_chain  the gateway's DebitMyData mainnet node agrees with the public explorer: on the
             height, and on the block a little below it (BLOCK_CHECK_DEPTH); different blocks
             at the same height mean one of them is on another chain, which alerts after
             BLOCK_ALERT_AFTER checks in a row, since a race between two new blocks settles
             by itself
  dmd_forks  the node's own branches (getchaintips) that ended in the last
             RECENT_BRANCH_BLOCKS blocks: a reorganization of BRANCH_ALERT_BLOCKS blocks or
             more (critical from the DMD payments' confirmations), a competing branch, invalid
             blocks; each one is sent once
  audit      alerts recorded by the worker (a payment reorganized out of the chain after its
             DID or receipt was issued), payments that need a refund, and every DID suspended,
             reactivated or revoked with dmd-gateway-admin
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
from dmd_gateway.db.models import (
    AgentIdentity,
    AlertState,
    AuditEvent,
    DirectoryListing,
    PaymentIntent,
    X402Authorization,
)
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.domain.routes import RailConfig
from dmd_gateway.monitoring.alerts import Finding, Severity, redact
from dmd_gateway.monitoring.state import (
    AUDIT_CURSOR,
    DMD_BLOCK,
    RATE_LIMITED,
    RECEIVING,
    WORKER_HEARTBEAT,
    get_state,
    put_state,
)
from dmd_gateway.pricing.mining_cost import BLOCK_HASH, DmdExplorerFigures, latest_record
from dmd_gateway.runtime import Runtime
from dmd_gateway.services import directory
from dmd_gateway.services.identity_status import STATUS_CHANGED
from dmd_gateway.services.owner_changes import OWNER_CHANGED, OWNER_NAMES
from dmd_gateway.settings import Settings

C, W = Severity.CRITICAL, Severity.WARNING
DMD_RAIL = "dmd_pow_native"
DEFAULT_PRICE_AGE = timedelta(seconds=900)
X402_UNKNOWN_AFTER = timedelta(minutes=30)
CONFIRMING_TOO_LONG = timedelta(hours=2)
HEIGHT_TOLERANCE = 3  # blocks between the node and the explorer
BLOCK_CHECK_DEPTH = 2  # blocks below the lower of the two tips, where their blocks are compared
BLOCK_ALERT_AFTER = 2  # checks in a row (every 2 minutes) with different blocks there
BRANCH_ALERT_BLOCKS = 2  # a shorter branch is one stale block, which happens on any chain
RECENT_BRANCH_BLOCKS = 60  # about 2.5 hours of DMD blocks; older branches are not reported
DMD_CONFIRMATIONS = 6  # when the DMD payment option does not say
RATE_LIMIT_ALERT_AFTER = 3  # checks in a row (every 2 minutes) refused by a rate limit
AUDIT_BATCH = 200
SHOWN_IDS = 5
CONFIRMING = (PaymentStatus.DETECTED, PaymentStatus.CONFIRMING, PaymentStatus.VERIFIED)
AUDIT_TITLES = {
    "alert.issued_identity_payment_reorged": (
        "A DID-LLM's payment left the chain after the DID-LLM was issued"
    ),
    "alert.completed_purchase_payment_reorged": (
        "A donation's or product's payment left the chain after its receipt was issued"
    ),
    "alert.issued_identity_payment_taken_back": (
        "A DID-LLM's card or PayPal payment was refunded or disputed after it was issued"
    ),
}


@dataclass
class MonitorContext:
    settings: Settings
    environ: Mapping[str, str]
    runtime: Runtime
    http: httpx.Client
    dmd_rpc: Callable[[], Any] | None = None  # returns a client with .call(method, params)
    # The daily report's discovery check (dmd_gateway.handshake), when it runs one.
    handshake: Callable[[], Any] | None = None

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
                f"Last sign of life: {seen}. Payments are not being verified, so DID-LLMs and "
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
    stored = get_state(session, RATE_LIMITED)
    refused_before = stored[0] if stored else {}
    refused: dict[str, int] = {}
    for rail_id, adapter in ctx.runtime.adapters.items():
        rail = ctx.runtime.registry.rails[rail_id]
        key = f"rails.{rail_id}"
        try:
            adapter.check_ready()
        except AdapterUnavailable as exc:
            detail = str(exc)
            if exc.rate_limited:
                refused[rail_id] = int(refused_before.get(rail_id, 0)) + 1
                # A short burst of refusals passes by itself: no alert unless it lasts (or
                # an alert for this option is already open).
                if refused[rail_id] < RATE_LIMIT_ALERT_AFTER and not _open(session, key):
                    continue
                detail += (
                    f" ({refused[rail_id]} checks in a row; if it lasts, the provider's "
                    "plan or key may need raising)"
                )
            findings.append(
                Finding(
                    key,
                    C,
                    f"{rail.asset.symbol} payments ({rail_id}) cannot be verified",
                    f"{detail}\nPayments with this option are not confirmed until this is "
                    f"fixed. To stop new quotes for it, set its kill switch "
                    f"({rail.kill_env}=true) and restart the gateway.",
                )
            )
    if refused or refused_before:
        put_state(session, RATE_LIMITED, refused, now)
    return findings


def _open(session: Session, key: str) -> bool:
    row = session.get(AlertState, key)
    return row is not None and row.resolved_at is None


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


DMD_KILL_SWITCH = (
    "stop DMD payments at once: DMD_RAIL_DMD_POW_NATIVE_KILL=true in "
    "/etc/dmd-gateway/gateway.env, then systemctl restart dmd-gateway-api (docs/emergencies.md, "
    "part 3)"
)


def _dmd_mainnet(ctx: MonitorContext) -> RailConfig | None:
    """The DMD payment option when it is on, on mainnet (the explorer is mainnet's)."""
    rail = ctx.runtime.registry.rails.get(DMD_RAIL)
    if DMD_RAIL not in ctx.runtime.adapters or rail is None or rail.network != "dmd:mainnet":
        return None
    return rail


def check_dmd_chain(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    if _dmd_mainnet(ctx) is None:
        return []
    rpc = ctx.rpc()
    node = int(rpc.call("getblockchaininfo")["blocks"])
    explorer = DmdExplorerFigures(ctx.http, ctx.settings.dmd_explorer_url)
    explorer_height = explorer.read().height
    findings = []
    if abs(node - explorer_height) > HEIGHT_TOLERANCE:
        behind = "behind" if node < explorer_height else "ahead of"
        findings.append(
            Finding(
                "dmd_chain.height",
                W,
                f"The gateway's DebitMyData node is {abs(node - explorer_height)} blocks "
                f"{behind} the explorer",
                f"Node: block {node}. Public explorer: block {explorer_height}. A node behind "
                "sees DMD payments late; a large difference can also mean the node or the "
                "explorer is on another chain.",
            )
        )
    height = min(node, explorer_height) - BLOCK_CHECK_DEPTH
    if height >= 1:
        findings.extend(_compare_blocks(rpc, explorer, height, session, now))
    return findings


def _compare_blocks(
    rpc: Any, explorer: DmdExplorerFigures, height: int, session: Session, now: datetime
) -> list[Finding]:
    """The node's and the explorer's blocks at the same height, a little below both tips."""
    ours = str(rpc.call("getblockhash", {"height": height})).strip().lower()
    if not BLOCK_HASH.fullmatch(ours):
        raise AdapterUnavailable(f"the DebitMyData node gave no block hash at height {height}")
    theirs = explorer.block_hash(height)
    stored = get_state(session, DMD_BLOCK)
    before = int(stored[0].get("checks", 0)) if stored else 0
    if ours == theirs:
        if before:
            put_state(session, DMD_BLOCK, {}, now)
        return []
    checks = before + 1
    put_state(session, DMD_BLOCK, {"checks": checks, "height": height}, now)
    if checks < BLOCK_ALERT_AFTER and not _open(session, "dmd_chain.block"):
        return []  # two new blocks racing at the tip: one wins within a minute or two
    return [
        Finding(
            "dmd_chain.block",
            C,
            "The gateway's DebitMyData node and the explorer are on different chains",
            f"At block {height}, the node has {ours} and the public explorer {theirs} "
            f"({checks} checks in a row). One of them follows another chain since a block "
            f"below {height}. Ask a third node, such as the DebitMyData pool's, for the same "
            f"block (dmd-cli getblockhash {height}). If the gateway's node is the one alone, "
            f"{DMD_KILL_SWITCH}.",
        )
    ]


def check_dmd_forks(ctx: MonitorContext, session: Session, now: datetime) -> list[Finding]:
    rail = _dmd_mainnet(ctx)
    if rail is None:
        return []
    tips = ctx.rpc().call("getchaintips")
    active = max(int(tip["height"]) for tip in tips if tip.get("status") == "active")
    confirmations = rail.confirmation_policy.required_confirmations or DMD_CONFIRMATIONS
    findings = []
    for tip in tips:
        status, height = tip.get("status"), int(tip["height"])
        length, tip_hash = int(tip["branchlen"]), str(tip.get("hash", ""))
        fork = height - length  # the last block the branch shares with the node's chain
        if status == "active" or height < active - RECENT_BRANCH_BLOCKS:
            continue
        branch = f"Its last block: {height} ({tip_hash}). The node's chain: block {active}."
        if status == "valid-fork" and length >= BRANCH_ALERT_BLOCKS:
            # Blocks the node had in its chain, then left for a branch with more work.
            deep = length >= confirmations
            findings.append(
                Finding(
                    f"dmd_forks.reorg.{fork}" + (".deep" if deep else ""),
                    C if deep else W,
                    f"The DebitMyData chain was reorganized: {length} blocks after block "
                    f"{fork} were replaced",
                    f"{branch} "
                    + (
                        f"DMD payments are settled after {confirmations} confirmations, so a "
                        "payment the gateway took as final may be gone; the worker alerts for "
                        "each one it issued something for. On a chain with little mining power "
                        "this can be an attack (a double spend): "
                        f"{DMD_KILL_SWITCH}, and find out who mined the replacing blocks."
                        if deep
                        else f"No settled payment can have left: DMD payments need "
                        f"{confirmations} confirmations, and payments still confirming in "
                        "those blocks wait for them again. Two or more blocks replaced at "
                        "once is rare: someone mined a chain of their own. If it happens "
                        "again, look at who mined the replacing blocks."
                    ),
                    kind="event",
                )
            )
        elif status in ("valid-headers", "headers-only") and length >= BRANCH_ALERT_BLOCKS:
            if fork >= active:
                continue  # the node's own chain, ahead of it: it is still downloading blocks
            heavier = height > active
            findings.append(
                Finding(
                    f"dmd_forks.competing.{fork}",
                    W,
                    f"A competing DebitMyData branch of {length} blocks, from block {fork}",
                    f"{branch} Someone mines another chain from block {fork}. "
                    + (
                        "It is longer than the node's own, which the node has not switched "
                        "to (yet): if it does, that is a reorganization, and an alert follows."
                        if heavier
                        else "It is shorter, so the node stays on its own chain; a longer one "
                        "later would replace the node's blocks after block "
                        f"{fork}."
                    ),
                    kind="event",
                )
            )
        elif status == "invalid":
            findings.append(
                Finding(
                    f"dmd_forks.invalid.{fork}",
                    W,
                    f"Invalid DebitMyData blocks after block {fork}",
                    f"{branch} The node refused {length} block{'s' if length > 1 else ''} "
                    "that break the chain's rules. A miner on another version of DebitMyData "
                    "Core, or a broken one: if more follow, compare the nodes' versions.",
                    kind="event",
                )
            )
    return findings


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
            AuditEvent.action.like("alert.%")
            | AuditEvent.action.in_(
                ("payment.refund_due", STATUS_CHANGED, OWNER_CHANGED, *directory.REPORTED)
            ),
        )
        .order_by(AuditEvent.id)
        .limit(AUDIT_BATCH)
    ).all()
    findings = []
    for event in events:
        if event.action == "payment.refund_due":
            findings.append(_refund_finding(session, event))
        elif event.action == STATUS_CHANGED:
            findings.append(_status_finding(session, event))
        elif event.action == OWNER_CHANGED:
            findings.append(_owner_finding(session, event))
        elif event.action in directory.REPORTED:
            findings.append(_directory_finding(ctx, session, event))
        else:
            data = json.dumps(event.data, sort_keys=True)
            when = event.occurred_at.strftime("%Y-%m-%d %H:%M")
            findings.append(
                Finding(
                    f"audit.{event.id}",
                    C,
                    AUDIT_TITLES.get(event.action, f"Alert recorded: {event.action}"),
                    f"{event.subject_type} {event.subject_id}, {when} UTC.\n{data}\nThe "
                    "payment is watched again; if it does not come back on chain, the DID-LLM or "
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


STATUS_WORDS = {"suspended": "suspended", "active": "reactivated", "revoked": "revoked"}


def _status_finding(session: Session, event: AuditEvent) -> Finding:
    data = event.data
    identity = session.get(AgentIdentity, event.subject_id)
    name = identity.display_name if identity else event.subject_id
    word = STATUS_WORDS.get(str(data.get("to")), f"set to {data.get('to')}")
    when = event.occurred_at.strftime("%Y-%m-%d %H:%M")
    if event.actor_type == "agent":  # its owner, with a signed message (owner_changes.py)
        who = f"by its owner, signed with {event.actor_id}"
        check = "Nothing to do: a revocation by the owner is their own choice, and final."
    else:
        who = f"by {event.actor_id} (dmd-gateway-admin)"
        check = "If nobody on the team made this change, someone else has access to the server."
    return Finding(
        f"audit.{event.id}",
        W,
        f"A DID-LLM was {word}: {name}",
        f"{data.get('did')} went from {data.get('from')} to {data.get('to')} on {when} UTC, "
        f"{who}.\nReason: {redact(str(data.get('reason')))}\n{check}",
        kind="event",
    )


def _owner_finding(session: Session, event: AuditEvent) -> Finding:
    """An owner change, signed by a current owner and the new ones: reported so that someone
    sees it, as a key change is often the first sign of a stolen key."""
    data = event.data
    identity = session.get(AgentIdentity, event.subject_id)
    name = identity.display_name if identity else event.subject_id
    when = event.occurred_at.strftime("%Y-%m-%d %H:%M")

    def owners(found: object) -> str:
        items = found.items() if isinstance(found, dict) else []
        return ", ".join(f"{OWNER_NAMES.get(k, k)} {v}" for k, v in items) or "-"

    return Finding(
        f"audit.{event.id}",
        W,
        f"A DID-LLM changed owners: {name}",
        f"{data.get('did')}, on {when} UTC, signed by a current owner and each new one.\n"
        f"Before: {owners(data.get('from'))}\nAfter: {owners(data.get('to'))}\n"
        "Earlier sign-ins ended. If its owner reports a stolen key, suspend it: "
        f'dmd-gateway-admin did suspend {data.get("did")} --reason "..." --by "<you>"',
        kind="event",
    )


DIRECTORY_WORDS = {
    directory.LISTED: "listed in the directory",
    directory.UPDATED: "changed its directory listing",
    directory.HIDDEN: "hidden from the directory",
    directory.SHOWN_AGAIN: "shown again in the directory",
}


def _directory_finding(ctx: MonitorContext, session: Session, event: AuditEvent) -> Finding:
    """A new or changed listing (so that someone reads it), or an operator's change."""
    identity = session.get(AgentIdentity, event.subject_id)
    name = identity.display_name if identity else event.subject_id
    did = str(event.data.get("did") or (identity.public_identifier if identity else ""))
    when = event.occurred_at.strftime("%Y-%m-%d %H:%M")
    title = f"A DID-LLM was {DIRECTORY_WORDS[event.action]}: {name}"
    if event.action in (directory.HIDDEN, directory.SHOWN_AGAIN):
        detail = (
            f"{did}, on {when} UTC, by {event.actor_id} (dmd-gateway-admin).\n"
            f"Reason: {redact(str(event.data.get('reason')))}\n"
            "If nobody on the team made this change, someone else has access to the server."
        )
    else:
        listing = session.get(DirectoryListing, event.subject_id)
        page = f"{directory.pages_base(ctx.settings)}/directory/{did.rsplit(':', 1)[-1]}"
        profile = ""
        if listing is not None and listing.listed:
            profile = (
                f"Specialty: {redact(listing.specialty or '')}\n"
                f"Description: {redact(listing.description or '')}\n"
            )
        detail = (
            f"{did}, on {when} UTC, signed by its owner.\n{profile}Check it: {page}\n"
            f'To hide it: dmd-gateway-admin directory hide {did} --reason "..." --by "<you>"'
        )
    return Finding(f"audit.{event.id}", W, title, detail, kind="event")


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
    "dmd_forks": check_dmd_forks,
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
