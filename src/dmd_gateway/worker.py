"""Background payment worker (GW-11).

Watches open payment intents on every enabled rail and moves them through the normalized
statuses. Several can run side by side, each with its own share of the payments (``Shard``):

    dmd-gateway-worker                  # one worker, every payment, forever
    dmd-gateway-worker --processes 4    # four, each with a quarter, watched over by this one
    dmd-gateway-worker --once           # single pass, e.g. from cron or a test

A worker pauses ``worker_interval_seconds`` between passes, except while more payments are
waiting than one pass takes (``BATCH_SIZE``): then it goes on at once, but never checks the
same payment more often than that. Rails checked through another company's API keep to their
pace (``checks_per_second``), shared between the workers. The first worker also refreshes the
DMD price. Each one records its own heartbeat, which the monitor checks.
"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import FrameType

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.adapters import AdapterUnavailable
from dmd_gateway.db.session import build_engine, session_factory, transaction
from dmd_gateway.monitoring.state import WORKER_HEARTBEAT, put_state
from dmd_gateway.observability import configure_logging
from dmd_gateway.pricing.mining_cost import PriceRefresher
from dmd_gateway.runtime import build_runtime
from dmd_gateway.services import settlement
from dmd_gateway.services.settlement import RailPacer, Shard, run_pass
from dmd_gateway.settings import get_settings, runtime_environ

log = logging.getLogger("dmd_gateway.worker")

MAX_PROCESSES = 32


def shard_argument(text: str) -> Shard:
    index, _, count = text.partition("/")
    try:
        return Shard(int(index), int(count))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text}: expected INDEX/COUNT, such as 0/4") from exc


def heartbeat_key(shard: Shard) -> str:
    """Where a worker records its heartbeat: the first one where a single worker always did."""
    return WORKER_HEARTBEAT if shard.index == 0 else f"{WORKER_HEARTBEAT}.{shard.index}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DebitMyData Agent Gateway payment worker")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument(
        "--processes",
        type=int,
        default=1,
        choices=range(1, MAX_PROCESSES + 1),
        metavar=f"1-{MAX_PROCESSES}",
        help="run this many workers side by side, each with its share of the payments",
    )
    parser.add_argument("--shard", type=shard_argument, default=Shard(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.processes > 1:
        return supervise(args.processes, once=args.once)
    return work(args.shard, once=args.once)


def supervise(count: int, *, once: bool) -> int:
    """Runs ``count`` workers, one per share, and stops them all if one stops by itself (the
    service manager then starts them all again: a share is never left unchecked)."""
    configure_logging(get_settings().log_level)
    children = [
        subprocess.Popen(  # noqa: S603 (this module itself, with arguments it builds)
            [
                sys.executable,
                "-m",
                "dmd_gateway.worker",
                "--shard",
                f"{index}/{count}",
                *(["--once"] if once else []),
            ]
        )
        for index in range(count)
    ]
    log.info("payment workers started", extra={"event": {"processes": count}})
    stopping = False

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    failed = False
    while any(child.poll() is None for child in children):
        if not stopping and any(child.poll() for child in children):  # one failed
            log.error(
                "a payment worker stopped: stopping the others",
                extra={"event": {"exit_codes": [child.poll() for child in children]}},
            )
            failed = True
            stop(signal.SIGTERM, None)
        time.sleep(0.2)
    if not stopping and any(child.returncode for child in children):
        failed = True
    return 1 if failed else 0


def work(shard: Shard, *, once: bool) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    environ = runtime_environ()
    runtime = build_runtime(settings, environ)
    # The DMD price is the first worker's job alone.
    prices = PriceRefresher.from_settings(settings, environ, runtime.registry)
    if shard.index != 0:
        prices = None
    if not runtime.adapters and prices is None:
        log.warning("no payment rail is enabled and no price to compute; nothing to do")
        return 0

    for rail_id, adapter in runtime.adapters.items():
        try:
            adapter.check_ready()
            log.info("payment rail ready", extra={"event": {"rail": rail_id, "shard": str(shard)}})
        except AdapterUnavailable as exc:
            log.error(
                "payment rail not ready", extra={"event": {"rail": rail_id, "error": str(exc)}}
            )

    engine = build_engine(settings)
    factory = session_factory(engine)
    rest = timedelta(seconds=settings.worker_interval_seconds)
    pacer = RailPacer(runtime.adapters, shard.count)
    stopping = False

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        log.info("worker stopping", extra={"event": {"signal": signum, "shard": str(shard)}})

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while not stopping:
            now = datetime.now(UTC)
            if prices is not None and prices.due(now):
                prices.run(factory, now)
            result = None
            if runtime.adapters:
                result = run_pass(
                    factory, runtime.adapters, now, shard=shard, rest=rest, pacer=pacer
                )
                if result.processed or result.unavailable or result.errors:
                    log.info(
                        "worker pass", extra={"event": {**result.__dict__, "shard": str(shard)}}
                    )
            _heartbeat(factory, now, result.__dict__ if result else {}, shard)
            if once:
                break
            if result is not None and result.due >= settlement.BATCH_SIZE:
                continue  # more are waiting: no pause
            for _ in range(int(settings.worker_interval_seconds * 10)):
                if stopping:
                    break
                time.sleep(0.1)
    finally:
        engine.dispose()
    return 0


def _heartbeat(
    factory: sessionmaker[Session],
    now: datetime,
    last_pass: dict[str, int],
    shard: Shard | None = None,
) -> None:
    """Tells the monitor the worker is alive (it alerts when this stops); the first one also
    says how many there are, so that the monitor checks each one."""
    shard = shard or Shard()
    value = {"last_pass": last_pass, **({"shards": shard.count} if shard.index == 0 else {})}
    try:
        with transaction(factory) as session:
            put_state(session, heartbeat_key(shard), value, now)
    except SQLAlchemyError as exc:
        log.error("worker heartbeat not recorded", extra={"event": {"error": type(exc).__name__}})


if __name__ == "__main__":
    raise SystemExit(main())
