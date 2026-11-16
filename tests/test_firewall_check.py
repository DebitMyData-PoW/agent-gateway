"""The firewall check from outside (scripts/firewall-check.sh, roadmap SEC-10), against this
computer: a port that answers is reported, the SSH port must answer and accept keys only (a
stand-in ssh says what a server would), and Cloudflare's names are refused (the check is for
the server's own address)."""

from __future__ import annotations

import os
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


def run(*args: str | int, ssh: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "FIREWALL_CHECK_SSH": str(ssh or "no-ssh-here")}
    return subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [str(BASH), str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


def fake_ssh(tmp_path: Path, answer: str) -> Path:
    """An ssh that answers as a server would to a login with nothing offered, and keeps the
    arguments it was given."""
    script = tmp_path / "ssh"
    script.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{tmp_path}/args"\necho "{answer}" >&2\nexit 255\n'
    )
    script.chmod(0o755)
    return script


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
    assert "SSH logins: not checked (ssh is not installed here)" in result.stdout
    assert result.stdout.strip().endswith("only SSH answers from outside, with keys only.")


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


def test_the_ssh_port_must_accept_keys_only(listening: list[int], tmp_path: Path) -> None:
    ssh, _, free = listening
    keys = fake_ssh(tmp_path, "root@127.0.0.1: Permission denied (publickey).")
    result = run("127.0.0.1", ssh, "--ports", f"{free}", ssh=keys)
    assert result.returncode == 0, result.stderr
    assert "  SSH logins: publickey (keys only, as expected)" in result.stdout
    asked = (tmp_path / "args").read_text().splitlines()
    assert asked[:2] == ["-p", str(ssh)] and asked[-2:] == ["root@127.0.0.1", "true"]
    for option in ("BatchMode=yes", "PubkeyAuthentication=no", "PasswordAuthentication=no"):
        assert option in asked  # nothing offered, nothing asked: the server only says which

    passwords = fake_ssh(tmp_path, "root@127.0.0.1: Permission denied (publickey,password).")
    result = run("127.0.0.1", ssh, "--ports", f"{free}", ssh=passwords)
    assert result.returncode == 1
    assert "SSH logins: publickey,password  NOT EXPECTED: a password can be guessed" in (
        result.stdout
    )

    silent = fake_ssh(tmp_path, "ssh: connect to host 127.0.0.1: Connection timed out")
    result = run("127.0.0.1", ssh, "--ports", f"{free}", ssh=silent)
    assert result.returncode == 0
    assert "SSH logins: the server did not say which" in result.stdout
