"""dmd-gateway-monitor: health checks and alerts, and the daily reconciliation report.

    dmd-gateway-monitor check    run every check once and send what changed
                                 (a systemd timer runs it every 2 minutes)
    dmd-gateway-monitor report   the daily reconciliation report (a timer, every day at 07:00 UTC)
    dmd-gateway-monitor test     send a test message on every configured channel

See docs/monitoring.md for the settings and what each alert means.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import httpx
from sqlalchemy.exc import SQLAlchemyError

from dmd_gateway.db.session import build_engine, session_factory, transaction
from dmd_gateway.monitoring.alerts import (
    AlertDispatcher,
    DeliveryFailed,
    FallbackState,
    Message,
    deliver,
    redact,
)
from dmd_gateway.monitoring.checks import MonitorContext, run_checks
from dmd_gateway.monitoring.reconcile import build_report
from dmd_gateway.observability import configure_logging
from dmd_gateway.runtime import build_runtime
from dmd_gateway.settings import Settings, get_settings, runtime_environ

log = logging.getLogger("dmd_gateway.monitor")
USER_AGENT = "DebitMyData-Agent-Gateway (monitor)"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DebitMyData Agent Gateway monitor")
    parser.add_argument("command", choices=("check", "report", "test"), nargs="?", default="check")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    environ = runtime_environ()
    try:
        dispatcher = AlertDispatcher.from_settings(settings, environ)
    except ValueError as exc:
        # The alert settings are wrong: say which one, and send nothing.
        log.error("alert settings", extra={"event": {"error": str(exc)}})
        print(f"Alert settings: {exc}")
        return 2
    if args.command == "test":
        return _test(settings, dispatcher)
    return run(args.command, settings, environ, dispatcher)


def _test(settings: Settings, dispatcher: AlertDispatcher) -> int:
    if not dispatcher.notifiers:
        print("No alert channel is configured: see docs/monitoring.md.")
        return 1
    message = Message(
        subject=f"[DebitMyData Gateway {settings.environment}] Test message",
        body="If you read this, the gateway's alerts reach you on this channel.\n",
    )
    names = ", ".join(n.name for n in dispatcher.notifiers)
    delivered = deliver(dispatcher.notifiers, message)
    print(f"Test message {'sent' if delivered else 'NOT sent'} ({names}); see the log for errors.")
    return 0 if delivered else 1


def run(
    command: str,
    settings: Settings,
    environ: Mapping[str, str],
    dispatcher: AlertDispatcher,
    *,
    http: httpx.Client | None = None,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    fallback = FallbackState(settings.monitor_fallback_state_path)
    prefix = f"[DebitMyData Gateway {settings.environment}]"
    client = http or httpx.Client(
        timeout=15, follow_redirects=False, headers={"User-Agent": USER_AGENT}
    )
    try:
        runtime = build_runtime(settings, environ)
    except Exception as exc:  # noqa: BLE001 - reported, then the run stops
        _outside_database(
            fallback,
            dispatcher,
            "configuration",
            now,
            f"{prefix} CRITICAL: The gateway's configuration cannot be loaded",
            f"{type(exc).__name__}: {redact(str(exc))[:400]}\nThe gateway refuses to start "
            "with this configuration.",
        )
        return 1
    ctx = MonitorContext(settings, environ, runtime, client)
    engine = build_engine(settings)
    try:
        with transaction(session_factory(engine)) as session:
            if command == "check":
                findings, completed = run_checks(ctx, session, now)
                dispatcher.apply(session, findings, completed, now)
            else:
                report = build_report(ctx, session, now)
                dispatcher.apply(session, report.findings, set(), now)
                if not deliver(dispatcher.notifiers, report.message(settings.environment)):
                    raise DeliveryFailed("daily report")
    except DeliveryFailed as exc:
        log.error("monitor messages not delivered", extra={"event": {"subject": str(exc)}})
        return 1
    except SQLAlchemyError as exc:
        _outside_database(
            fallback,
            dispatcher,
            "database",
            now,
            f"{prefix} CRITICAL: The gateway database is unreachable",
            f"{type(exc).__name__}. Quotes, payments and registrations stop until it is "
            "back. On the server: systemctl status postgresql",
        )
        return 1
    finally:
        engine.dispose()
        if http is None:
            client.close()
    for key, title in (
        ("database", "The gateway database is reachable again"),
        ("configuration", "The gateway's configuration loads again"),
    ):
        if fallback.clear(key):
            deliver(dispatcher.notifiers, Message(subject=f"{prefix} Resolved: {title}", body=""))
    return 0


def _outside_database(
    fallback: FallbackState,
    dispatcher: AlertDispatcher,
    key: str,
    now: datetime,
    subject: str,
    body: str,
) -> None:
    """Alerts that cannot use the alert states in the database: at most once per repeat
    delay, remembered in a small local file."""
    log.error("monitor: %s", key, extra={"event": {"detail": body}})
    if not fallback.due(key, now, dispatcher.repeat_critical):
        return
    message = Message(subject=subject, body=body + "\n", critical=True)
    if deliver(dispatcher.notifiers, message):
        fallback.sent(key, now)


if __name__ == "__main__":
    raise SystemExit(main())
