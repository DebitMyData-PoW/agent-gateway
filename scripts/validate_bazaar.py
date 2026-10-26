#!/usr/bin/env python3
"""Check the gateway's paid routes with Coinbase's x402 Bazaar validator.

The directive asks to validate each public paid endpoint before launch. Coinbase's validator
calls the endpoint with the example request it advertises, expects a 402 with a valid
``bazaar`` extension, and reports whether the CDP facilitator can index it. It needs no API
key and changes nothing. Indexing itself happens after the first payment settled through
the CDP facilitator.

The paid routes are the DID registration and the inference donation; give --route to check
only one of them.

Usage:
    python scripts/validate_bazaar.py https://api.debitmydata.io
    python scripts/validate_bazaar.py https://api.debitmydata.io --route /v1/donations/inference

Exit code 0 when every route is valid, 1 when one is not, 2 when the validator cannot be
reached.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

VALIDATOR = "https://api.cdp.coinbase.com/platform/v2/x402/validate"
ROUTE = "/v1/dids/registrations"
ROUTES = (ROUTE, "/v1/donations/inference")


def resource_url(base_url: str, route: str = ROUTE) -> str:
    base = base_url.strip().rstrip("/")
    if not base.startswith("https://"):
        raise SystemExit("The Bazaar only lists public https endpoints: give an https:// URL.")
    return f"{base}{route}"


def validate(
    base_url: str, route: str = ROUTE, *, transport: httpx.BaseTransport | None = None
) -> tuple[bool, dict[str, Any]]:
    """Whether the validator accepts the route, and its full answer."""
    with httpx.Client(timeout=60, transport=transport) as client:
        response = client.post(
            VALIDATOR, json={"resource": resource_url(base_url, route), "method": "POST"}
        )
    try:
        answer = response.json()
    except ValueError as exc:
        raise ConnectionError(f"validator answered HTTP {response.status_code}") from exc
    if not isinstance(answer, dict):
        raise ConnectionError(f"validator answered HTTP {response.status_code}")
    outcome = (answer.get("simulation") or {}).get("outcome")
    return answer.get("valid") is True and outcome == "accepted", answer


def _lines(title: str, value: Any) -> list[str]:
    if not value:
        return []
    return [f"{title}:", *("  " + line for line in json.dumps(value, indent=2).splitlines())]


def report(ok: bool, answer: dict[str, Any]) -> str:
    lines = [
        f"Route valid: {'yes' if ok else 'NO'}",
        f"Simulation outcome: {(answer.get('simulation') or {}).get('outcome', '-')}",
    ]
    lines += _lines("Preflight (required failures and advice)", answer.get("preflight"))
    lines += _lines("Bazaar metadata as the validator sees it", answer.get("bazaarExtension"))
    if not ok:
        lines += _lines("Full answer", answer)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "base_url", help="the gateway's public URL, e.g. https://api.debitmydata.io"
    )
    parser.add_argument(
        "--route", choices=ROUTES, help="check only this route (default: every paid route)"
    )
    args = parser.parse_args(argv)
    all_ok = True
    for route in (args.route,) if args.route else ROUTES:
        try:
            ok, answer = validate(args.base_url, route)
        except (httpx.HTTPError, ConnectionError) as exc:
            print(f"Could not reach the validator: {exc}", file=sys.stderr)
            return 2
        print(f"== POST {route}")
        print(report(ok, answer))
        all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
