"""The payment worker's command: several workers side by side (``--processes``), each with its
own share of the payments (worker.py, services/settlement.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dmd_gateway.services.settlement import Shard
from dmd_gateway.worker import heartbeat_key, main, shard_argument

ROOT = Path(__file__).resolve().parents[1]


def test_shares_and_their_heartbeats() -> None:
    assert shard_argument("2/4") == Shard(2, 4)
    assert str(Shard(2, 4)) == "2/4"
    assert heartbeat_key(Shard(0, 4)) == "heartbeat.worker"  # where one worker always wrote
    assert heartbeat_key(Shard(3, 4)) == "heartbeat.worker.3"
    for wrong in ("4/4", "1", "a/b", "-1/2"):
        with pytest.raises(SystemExit):
            main(["--shard", wrong, "--once"])
    with pytest.raises(SystemExit):
        main(["--processes", "0"])


def test_several_workers_start_and_end_together() -> None:
    """Started as processes of their own, each with its share; with nothing to check (no
    payment option enabled), each one ends at once, and so does the command, without error."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DMD_GW_", "DMD_RAIL_"))
    }
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")]))
    result = subprocess.run(  # noqa: S603 (fixed arguments, this repository's own module)
        [sys.executable, "-m", "dmd_gateway.worker", "--processes", "3", "--once"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert '"processes": 3' in output or "payment workers started" in output
    assert output.count("nothing to do") == 3
