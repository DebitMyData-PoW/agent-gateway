"""The load-test script (scripts/load_test.py): its numbers, its safety checks, and a short
run against the gateway in memory."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load_test.py"
spec = importlib.util.spec_from_file_location("load_test", SCRIPT)
assert spec is not None and spec.loader is not None
load_test = importlib.util.module_from_spec(spec)
sys.modules["load_test"] = load_test
spec.loader.exec_module(load_test)


def test_percentiles_use_the_nearest_rank() -> None:
    values = [float(n) for n in range(1, 101)]
    assert load_test.percentile(values, 0.5) == 50
    assert load_test.percentile(values, 0.95) == 95
    assert load_test.percentile(values, 0.99) == 99
    assert load_test.percentile([], 0.5) == 0


def test_never_aimed_at_production_or_by_accident_elsewhere() -> None:
    with pytest.raises(SystemExit, match="production"):
        load_test.check_target("https://api.debitmydata.io", allow_remote=True)
    with pytest.raises(SystemExit, match="not this machine"):
        load_test.check_target("https://staging-api.debitmydata.io", allow_remote=False)
    load_test.check_target("https://staging-api.debitmydata.io", allow_remote=True)
    load_test.check_target("http://127.0.0.1:8000", allow_remote=False)


def test_limits_on_the_run_itself() -> None:
    with pytest.raises(SystemExit):
        load_test.parse_args(["--concurrency", "5000"])
    with pytest.raises(SystemExit):
        load_test.parse_args(["--duration", "3600"])


def test_a_short_run_counts_every_answer(client: TestClient) -> None:
    args = load_test.parse_args(["--scenario", "reads", "--concurrency", "2", "--duration", "1"])
    transport = httpx.ASGITransport(app=client.app)
    data = load_test.summary(asyncio.run(load_test.run(args, transport)))
    reads = data["by_kind"]
    assert set(reads) == {"discovery", "payment-options"}
    for row in reads.values():
        assert row["requests"] == row["ok"] > 0
        assert row["failed"] == row["rate_limited"] == 0
        assert row["median_ms"] <= row["p95_ms"] <= row["p99_ms"] <= row["max_ms"]
