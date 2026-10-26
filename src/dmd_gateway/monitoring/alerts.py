"""Alerts: what the monitor found, sent by email and Discord, without repeating itself.

A finding is either a *condition* (an ongoing problem, such as a stopped worker: sent when it
starts, again after a while if it lasts, and once more when it is resolved) or an *event*
(something that happened once, such as a payment reorganized out of the chain after its DID
was issued: sent once). Everything found in one run goes out as one message per channel, so a
bad hour gives a few messages, not hundreds.

Routing: every alert goes to ``alert_email_to`` and to Discord; critical ones also go to
``alert_email_critical_to`` and mention ``alert_discord_mention_user_id`` on Discord. The daily
report goes to ``report_email_to`` and Discord.

Texts never contain secrets: URLs are cut down to their host before anything is sent, since
a chain RPC address can carry an API key in its path.
"""

from __future__ import annotations

import json
import logging
import re
import smtplib
import ssl
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from dmd_gateway.db.models import AlertState
from dmd_gateway.settings import Settings, email_list

log = logging.getLogger(__name__)

DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
DISCORD_LIMIT = 2000
SUBJECT_LIMIT = 150
SMTP_TIMEOUT_SECONDS = 20
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    key: str  # stable: "<check>.<what>", e.g. "worker.stopped"
    severity: Severity
    title: str  # one line
    detail: str = ""
    kind: Literal["condition", "event"] = "condition"


@dataclass(frozen=True)
class Message:
    subject: str
    body: str
    critical: bool = False
    report: bool = False


class NotifyError(RuntimeError):
    """A channel could not deliver a message."""


class DeliveryFailed(RuntimeError):
    """No channel delivered the message."""


class Notifier(Protocol):
    name: str

    def send(self, message: Message) -> None: ...


def redact(text: str) -> str:
    """Cuts every URL down to its scheme and host."""

    def host_only(match: re.Match[str]) -> str:
        parts = urlsplit(match.group(0))
        return f"{parts.scheme}://{parts.hostname or '?'}/…"

    return _URL.sub(host_only, text)


# --------------------------------------------------------------------------- channels


class EmailNotifier:
    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        security: Literal["starttls", "ssl"],
        username: str,
        password: str,
        sender: str,
        to_all: Sequence[str],
        to_critical: Sequence[str],
        to_report: Sequence[str],
        smtp: Callable[..., smtplib.SMTP] = smtplib.SMTP,
        smtp_ssl: Callable[..., smtplib.SMTP] = smtplib.SMTP_SSL,
    ) -> None:
        self._host, self._port, self._security = host, port, security
        self._username, self._password, self._sender = username, password, sender
        self._to_all, self._to_critical, self._to_report = to_all, to_critical, to_report
        self._smtp, self._smtp_ssl = smtp, smtp_ssl

    def recipients(self, message: Message) -> list[str]:
        chosen = list(self._to_report) if message.report else list(self._to_all)
        if message.critical and not message.report:
            chosen += list(self._to_critical)
        return list(dict.fromkeys(chosen))  # in order, without duplicates

    def send(self, message: Message) -> None:
        recipients = self.recipients(message)
        if not recipients:
            return
        email = EmailMessage()
        email["From"] = self._sender
        email["To"] = ", ".join(recipients)
        email["Subject"] = message.subject[:SUBJECT_LIMIT]
        email.set_content(message.body)
        context = ssl.create_default_context()
        try:
            if self._security == "ssl":
                server = self._smtp_ssl(
                    self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS, context=context
                )
            else:
                server = self._smtp(self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS)
            with server:
                if self._security == "starttls":
                    server.starttls(context=context)
                if self._username:
                    server.login(self._username, self._password)
                server.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotifyError(f"email: {type(exc).__name__}: {redact(str(exc))[:200]}") from exc


class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str, mention_user_id: str, client: httpx.Client) -> None:
        parts = urlsplit(webhook_url)
        if (
            parts.scheme != "https"
            or parts.hostname not in DISCORD_HOSTS
            or not parts.path.startswith("/api/webhooks/")
        ):
            raise ValueError("the Discord webhook must be a https://discord.com/api/webhooks/ URL")
        self._url, self._mention, self._client = webhook_url, mention_user_id, client

    def send(self, message: Message) -> None:
        mention = self._mention if message.critical and not message.report else ""
        prefix = f"<@{mention}> " if mention else ""
        content = f"{prefix}**{message.subject}**\n{message.body}"
        if len(content) > DISCORD_LIMIT:
            content = content[: DISCORD_LIMIT - 30] + "\n… (cut; full text by email)"
        payload = {
            "content": content,
            # Only the one configured person can ever be notified, never @everyone.
            "allowed_mentions": {"parse": [], "users": [mention] if mention else []},
        }
        try:
            response = self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"discord: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise NotifyError(f"discord: HTTP {response.status_code}")


def notifiers_from_settings(
    settings: Settings, environ: Mapping[str, str], client: httpx.Client | None = None
) -> list[Notifier]:
    notifiers: list[Notifier] = []
    to_all = email_list(settings.alert_email_to)
    to_critical = email_list(settings.alert_email_critical_to)
    to_report = email_list(settings.report_email_to)
    if settings.alert_smtp_host and (to_all or to_critical or to_report):
        if not settings.alert_smtp_from:
            raise ValueError("DMD_GW_ALERT_SMTP_FROM is required to send alert emails")
        notifiers.append(
            EmailNotifier(
                host=settings.alert_smtp_host,
                port=settings.alert_smtp_port,
                security=settings.alert_smtp_security,
                username=settings.alert_smtp_username,
                password=environ.get("DMD_ALERT_SMTP_PASSWORD", ""),
                sender=settings.alert_smtp_from,
                to_all=to_all,
                to_critical=to_critical,
                to_report=to_report,
            )
        )
    webhook = (environ.get("DMD_ALERT_DISCORD_WEBHOOK_URL") or "").strip()
    if webhook:
        notifiers.append(
            DiscordNotifier(
                webhook,
                settings.alert_discord_mention_user_id,
                client or httpx.Client(timeout=15, follow_redirects=False),
            )
        )
    return notifiers


def deliver(notifiers: Sequence[Notifier], message: Message) -> bool:
    """Sends on every channel. True if at least one channel delivered it (or there is no
    channel at all: then the message is only logged)."""
    log.warning(
        "monitor message",
        extra={"event": {"subject": message.subject, "critical": message.critical}},
    )
    if not notifiers:
        return True
    delivered = False
    for notifier in notifiers:
        try:
            notifier.send(message)
            delivered = True
        except NotifyError as exc:
            log.error("alert not delivered", extra={"event": {"error": str(exc)}})
    return delivered


# --------------------------------------------------------------------------- de-duplication


@dataclass
class _Item:
    finding: Finding
    state: str  # "new", "ongoing" or "resolved"
    since: datetime


@dataclass
class AlertDispatcher:
    notifiers: Sequence[Notifier]
    environment: str
    repeat_critical: timedelta
    repeat_warning: timedelta
    sent: list[Message] = field(default_factory=list)

    @classmethod
    def from_settings(
        cls, settings: Settings, environ: Mapping[str, str], client: httpx.Client | None = None
    ) -> AlertDispatcher:
        return cls(
            notifiers_from_settings(settings, environ, client),
            settings.environment,
            timedelta(minutes=settings.alert_repeat_critical_minutes),
            timedelta(hours=settings.alert_repeat_warning_hours),
        )

    def _repeat(self, severity: str) -> timedelta:
        return self.repeat_critical if severity == Severity.CRITICAL else self.repeat_warning

    def apply(
        self,
        session: Session,
        findings: Iterable[Finding],
        completed_checks: set[str],
        now: datetime,
    ) -> Message | None:
        """Records this run's findings and sends what is new, still ongoing past its repeat
        delay, or resolved. Conditions of a check that could not run are left as they are."""
        items: list[_Item] = []
        rows: list[AlertState] = []
        seen: set[str] = set()
        for finding in findings:
            if finding.key in seen:
                continue
            seen.add(finding.key)
            row = session.get(AlertState, finding.key, with_for_update=True)
            if row is None or row.resolved_at is not None:
                if row is not None and finding.kind == "event":
                    continue  # an event is sent once, ever
                if row is None:
                    row = AlertState(key=finding.key, notifications=0)
                    session.add(row)
                row.kind, row.severity = finding.kind, finding.severity.value
                row.first_seen_at, row.resolved_at, row.last_sent_at = now, None, None
                items.append(_Item(finding, "new", now))
            elif row.last_sent_at is None or now - row.last_sent_at >= self._repeat(
                finding.severity
            ):
                items.append(_Item(finding, "ongoing", row.first_seen_at))
            row.title, row.detail = finding.title, redact(finding.detail)
            row.severity, row.last_seen_at = finding.severity.value, now
            if finding.kind == "event":
                row.resolved_at = now
            rows.append(row)

        open_rows = session.scalars(
            select(AlertState).where(
                AlertState.resolved_at.is_(None), AlertState.kind == "condition"
            )
        ).all()
        for row in open_rows:
            check = row.key.split(".", 1)[0]
            if row.key in seen or check not in completed_checks:
                continue
            row.resolved_at = now
            finding = Finding(row.key, Severity(row.severity), row.title, row.detail)
            items.append(_Item(finding, "resolved", row.first_seen_at))
            rows.append(row)

        if not items:
            return None
        message = self.compose(items)
        if not deliver(self.notifiers, message):
            # Nothing was delivered: the caller rolls this run back, so it is sent again on
            # the next run.
            raise DeliveryFailed(message.subject)
        self.sent.append(message)
        for item, row in _pairs(items, rows):
            if item.state != "resolved":
                row.last_sent_at = now
            row.notifications = (row.notifications or 0) + 1
        return message

    def compose(self, items: Sequence[_Item]) -> Message:
        critical = any(
            i.finding.severity == Severity.CRITICAL and i.state != "resolved" for i in items
        )
        active = [i for i in items if i.state != "resolved"]
        resolved = [i for i in items if i.state == "resolved"]
        prefix = f"[DMD Gateway {self.environment}]"
        if len(items) == 1:
            item = items[0]
            label = (
                "Resolved"
                if item.state == "resolved"
                else item.finding.severity.value.upper()
                + (" (still ongoing)" if item.state == "ongoing" else "")
            )
            subject = f"{prefix} {label}: {item.finding.title}"
        else:
            parts = []
            if active:
                parts.append(f"{len(active)} alert{'s' if len(active) > 1 else ''}")
            if resolved:
                parts.append(f"{len(resolved)} resolved")
            level = "CRITICAL" if critical else ("WARNING" if active else "Resolved")
            subject = f"{prefix} {level}: {', '.join(parts)}"

        lines: list[str] = []
        for item in active + resolved:
            finding = item.finding
            if item.state == "resolved":
                head = f"RESOLVED: {finding.title}"
                tail = f"Started {item.since:%Y-%m-%d %H:%M} UTC."
            else:
                head = f"{finding.severity.value.upper()}: {finding.title}"
                tail = (
                    f"Ongoing since {item.since:%Y-%m-%d %H:%M} UTC."
                    if item.state == "ongoing"
                    else ""
                )
            lines.append(head)
            if finding.detail and item.state != "resolved":
                lines.append(redact(finding.detail))
            if tail:
                lines.append(tail)
            lines.append(f"(alert key: {finding.key})")
            lines.append("")
        lines.append(
            "Sent by the DebitMyData Agent Gateway monitor. Runbook: docs/monitoring.md in "
            "the agent-gateway repository."
        )
        return Message(subject=subject, body="\n".join(lines).strip() + "\n", critical=critical)


def _pairs(items: Sequence[_Item], rows: Sequence[AlertState]) -> list[tuple[_Item, AlertState]]:
    by_key = {row.key: row for row in rows}
    return [(item, by_key[item.finding.key]) for item in items if item.finding.key in by_key]


# --------------------------------------------------------------------------- no database


class FallbackState:
    """Remembers, in a small file, when the "database unreachable" alert was last sent:
    without the database the alert states cannot be read."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def _read(self) -> dict[str, str]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, str]) -> None:
        if self._path is None:
            return
        try:
            self._path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            log.error("monitor fallback state not saved", extra={"event": {"error": str(exc)}})

    def due(self, key: str, now: datetime, repeat: timedelta) -> bool:
        last = self._read().get(key)
        if last is None:
            return True
        try:
            return now - datetime.fromisoformat(last) >= repeat
        except ValueError:
            return True

    def sent(self, key: str, now: datetime) -> None:
        data = self._read()
        data[key] = now.isoformat()
        self._write(data)

    def clear(self, key: str) -> bool:
        data = self._read()
        if key not in data:
            return False
        del data[key]
        self._write(data)
        return True
