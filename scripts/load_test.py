"""Load test for the Agent Gateway: how many requests per second it answers, and how fast.

Run it on the staging server against the API directly, to measure the gateway itself:

    .venv/bin/python scripts/load_test.py --base-url http://127.0.0.1:8000 --scenario mixed

Scenarios:
    reads   discovery, payment options and payment status (what most agents do most)
    quotes  registration quotes, each for a new agent key (writes to the database; a DMD
            quote also asks the DMD node for a new address)
    mixed   9 reads for every quote

Each run prints, per kind of request: how many were answered, how many were refused for
going too fast (429), how many failed, requests per second, and the response times (median,
95th and 99th percentile, slowest).

Every quote it creates is a real, unpaid quote that simply expires after 15 minutes. Never
run it against production. A run through Cloudflare (``--allow-remote``) shows the rate
limits at work, and Cloudflare may treat a long or heavy run as an attack.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import secrets
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dmd_gateway.identity.crypto import ED25519_PUB_MULTICODEC, multibase_encode

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PRODUCTION_HOSTS = {"api.debitmydata.io"}
QUOTES = "/v1/dids/registration-quotes"
READS = ("/v1/discovery", "/v1/payment-options")


@dataclass
class Results:
    started: float = field(default_factory=time.perf_counter)
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    payment_ids: list[str] = field(default_factory=list)

    def record(self, kind: str, status: str, seconds: float) -> None:
        self.latencies[kind].append(seconds)
        self.statuses[kind][status] += 1


def percentile(values: list[float], fraction: float) -> float:
    """The value below which ``fraction`` of the values fall (nearest rank)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def new_agent_key() -> str:
    raw = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return "ed25519:" + multibase_encode(ED25519_PUB_MULTICODEC + raw)


async def one_request(
    client: httpx.AsyncClient, results: Results, scenario: str, option: str
) -> None:
    quote = scenario == "quotes" or (scenario == "mixed" and secrets.randbelow(10) == 0)
    if quote:
        kind = "quote"
        call = client.post(
            QUOTES,
            json={
                "display_name": "LoadTest-01",
                "participant_type": "autonomous_agent",
                "controller_public_key": new_agent_key(),
                "preferred_payment_option": option,
            },
        )
    elif results.payment_ids and secrets.randbelow(3) == 0:
        kind = "payment status"
        call = client.get(f"/v1/payments/{secrets.choice(results.payment_ids)}")
    else:
        path = secrets.choice(READS)
        kind = path.rsplit("/", 1)[-1]
        call = client.get(path)
    started = time.perf_counter()
    try:
        response = await call
        status = str(response.status_code)
        if quote and response.status_code == 201 and len(results.payment_ids) < 1000:
            results.payment_ids.append(response.json()["payment_intent_id"])
    except httpx.HTTPError as exc:
        status = type(exc).__name__
    results.record(kind, status, time.perf_counter() - started)


async def run(
    args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None = None
) -> Results:
    results = Results()
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(
        base_url=args.base_url,
        timeout=args.timeout,
        limits=limits,
        trust_env=False,
        transport=transport,
    ) as client:
        if args.scenario in ("reads", "mixed"):  # a few payments for the status reads
            for _ in range(5):
                await one_request(client, results, "quotes", args.payment_option)
            results = Results(payment_ids=results.payment_ids)
        deadline = time.perf_counter() + args.duration

        async def worker() -> None:
            while time.perf_counter() < deadline:
                await one_request(client, results, args.scenario, args.payment_option)

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    return results


def summary(results: Results) -> dict[str, Any]:
    elapsed = time.perf_counter() - results.started
    rows = {}
    for kind, latencies in sorted(results.latencies.items()):
        statuses = results.statuses[kind]
        ok = sum(n for status, n in statuses.items() if status.startswith("2"))
        rows[kind] = {
            "requests": len(latencies),
            "ok": ok,
            "rate_limited": statuses.get("429", 0),
            "failed": len(latencies) - ok - statuses.get("429", 0),
            "per_second": round(len(latencies) / elapsed, 1),
            "median_ms": round(percentile(latencies, 0.5) * 1000, 1),
            "p95_ms": round(percentile(latencies, 0.95) * 1000, 1),
            "p99_ms": round(percentile(latencies, 0.99) * 1000, 1),
            "max_ms": round(max(latencies) * 1000, 1),
            "statuses": dict(statuses),
        }
    total = sum(len(v) for v in results.latencies.values())
    return {
        "seconds": round(elapsed, 1),
        "requests": total,
        "per_second": round(total / elapsed, 1),
        "by_kind": rows,
    }


def print_summary(data: dict[str, Any]) -> None:
    print(
        f"\n{data['requests']} requests in {data['seconds']} s: {data['per_second']} per second\n"
    )
    header = f"{'request':<16}{'count':>8}{'ok':>8}{'429':>7}{'failed':>8}{'/s':>8}"
    header += f"{'median':>9}{'p95':>9}{'p99':>9}{'max':>9}  (ms)"
    print(header)
    for kind, row in data["by_kind"].items():
        print(
            f"{kind:<16}{row['requests']:>8}{row['ok']:>8}{row['rate_limited']:>7}"
            f"{row['failed']:>8}{row['per_second']:>8}{row['median_ms']:>9}"
            f"{row['p95_ms']:>9}{row['p99_ms']:>9}{row['max_ms']:>9}"
        )
        others = {s: n for s, n in row["statuses"].items() if not s.startswith("2") and s != "429"}
        if others:
            print(f"{'':<16}failures: {others}")


def check_target(base_url: str, allow_remote: bool) -> None:
    host = urlsplit(base_url).hostname or ""
    if host in PRODUCTION_HOSTS:
        sys.exit(f"Refusing to load-test production ({host}).")
    if host not in LOCAL_HOSTS and not allow_remote:
        sys.exit(
            f"{host} is not this machine. Run the test on the server against "
            "http://127.0.0.1:8000, or add --allow-remote for a short run through Cloudflare."
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--scenario", choices=("reads", "quotes", "mixed"), default="mixed")
    parser.add_argument("--payment-option", default="dmd_pow_native")
    parser.add_argument("--concurrency", type=int, default=20, help="requests in flight")
    parser.add_argument("--duration", type=float, default=30, help="seconds")
    parser.add_argument("--timeout", type=float, default=30, help="seconds per request")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 1000 or not 1 <= args.duration <= 600:
        parser.error("use 1-1000 concurrent requests for 1-600 seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    check_target(args.base_url, args.allow_remote)
    data = summary(asyncio.run(run(args)))
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_summary(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
