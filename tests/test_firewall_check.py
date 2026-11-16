"""The firewall check from outside (scripts/firewall-check.sh, roadmap SEC-10), against this
computer: a port that answers is reported, the SSH port must answer, and Cloudflare's names
are refused (the check is for the server's own address)."""

from __future__ import annotations

import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "firewall-check.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which("timeout") is None, reason="needs bash and timeout"
)


def run(*args: str | int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [str(BASH), str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def listening() -> Iterator[list[int]]:
    """Two ports that answer on 127.0.0.1, and one that does not."""
    sockets = []
    for _ in range(2):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        sockets.append(server)
    unused = socket.socket()
    unused.bind(("127.0.0.1", 0))
    free = unused.getsockname()[1]
    unused.close()
    yield [server.getsockname()[1] for server in sockets] + [free]
    for server in sockets:
        server.close()


def test_only_ssh_answers(listening: list[int]) -> None:
    ssh, _, free = listening
    result = run("127.0.0.1", ssh, "--ports", f"{free}")
    assert result.returncode == 0, result.stderr
    assert f"  {ssh:<6} open    your SSH port, as expected" in result.stdout
    assert f"  {free:<6} closed" in result.stdout
    assert result.stdout.strip().endswith("FIREWALL CHECK PASSED: only SSH answers from outside.")


def test_an_open_port_or_a_silent_ssh_is_reported(listening: list[int]) -> None:
    ssh, other, free = listening
    result = run("127.0.0.1", ssh, "--ports", f"{other} {free}")
    assert result.returncode == 1
    assert f"  {other:<6} OPEN    not expected: anyone can reach it" in result.stdout
    assert "FIREWALL CHECK FAILED" in result.stderr

    wrong = run("[127.0.0.1]", free, "--ports", f"{free}")
    assert wrong.returncode == 1
    assert f"  {free:<6} closed  your SSH port: wrong address or port" in wrong.stdout


def test_the_server_is_named_by_its_own_address() -> None:
    refused = run("api.debitmydata.io", 22)
    assert refused.returncode == 2 and "Cloudflare's address" in refused.stderr
    assert run("127.0.0.1").returncode == 2
    assert run("127.0.0.1", "ssh").returncode == 2
