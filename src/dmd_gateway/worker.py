"""Background payment worker (GW-11).

Watches open payment intents on every enabled rail and moves them through the normalized
statuses. Run one worker per environment (several are safe: rows are locked per intent):

    dmd-gateway-worker            # loop forever
    dmd-gateway-worker --once     # single pass, e.g. from cron or a test
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from types import FrameType

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.adapters import AdapterUnavailable
from dmd_gateway.db.session import build_engine, session_factory, transaction
from dmd_gateway.monitoring.state import WORKER_HEARTBEAT, put_state
from dmd_gateway.observability import configure_logging
from dmd_gateway.pricing.mining_cost import PriceRefresher
from dmd_gateway.runtime import build_runtime
from dmd_gateway.services.settlement import run_pass
from dmd_gateway.settings import get_settings, runtime_environ

log = logging.getLogger("dmd_gateway.worker")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DebitMyData Agent Gateway payment worker")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    environ = runtime_environ()
    runtime = build_runtime(settings, environ)
    prices = PriceRefresher.from_settings(settings, environ, runtime.registry)
    if not runtime.adapters and prices is None:
        log.warning("no payment rail is enabled and no price to compute; nothing to do")
        return 0

    for rail_id, adapter in runtime.adapters.items():
        try:
            adapter.check_ready()
            log.info("payment rail ready", extra={"event": {"rail": rail_id}})
        except AdapterUnavailable as exc:
            log.error(
                "payment rail not ready", extra={"event": {"rail": rail_id, "error": str(exc)}}
            )

    engine = build_engine(settings)
    factory = session_factory(engine)
    stopping = False

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        log.info("worker stopping", extra={"event": {"signal": signum}})

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while not stopping:
            now = datetime.now(UTC)
            if prices is not None and prices.due(now):
                prices.run(factory, now)
            result = None
            if runtime.adapters:
                result = run_pass(factory, runtime.adapters, now)
                if result.processed or result.unavailable or result.errors:
                    log.info("worker pass", extra={"event": result.__dict__})
            _heartbeat(factory, now, result.__dict__ if result else {})
            if args.once:
                break
            for _ in range(int(settings.worker_interval_seconds * 10)):
                if stopping:
                    break
                time.sleep(0.1)
    finally:
        engine.dispose()
    return 0


def _heartbeat(factory: sessionmaker[Session], now: datetime, last_pass: dict[str, int]) -> None:
    """Tells the monitor the worker is alive (it alerts when this stops)."""
    try:
        with transaction(factory) as session:
            put_state(session, WORKER_HEARTBEAT, {"last_pass": last_pass}, now)
    except SQLAlchemyError as exc:
        log.error("worker heartbeat not recorded", extra={"event": {"error": type(exc).__name__}})


if __name__ == "__main__":
    raise SystemExit(main())
