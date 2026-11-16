"""The backups' restore test (scripts/backup-restore-test.sh), on a real pair made as
deploy/production/backup.sh makes it: a database dump and a settings archive, each encrypted
with age. It reads both with the offline key, names what is missing, counts each table's rows,
writes nothing decrypted to disk, and refuses a wrong key or a mismatched pair."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from .conftest import TEST_DATABASE_URL
from .test_registration import engine  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup-restore-test.sh"
TOOLS = {name: shutil.which(name) for name in ("bash", "age", "age-keygen", "pg_dump", "tar")}
NEEDED = (*TOOLS, "pg_restore")
STAMP = "20261116T031500Z"

pytestmark = [
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set"),
    pytest.mark.skipif(
        any(shutil.which(name) is None for name in NEEDED), reason="needs " + ", ".join(NEEDED)
    ),
]


def tool(name: str, *args: str | Path, data: bytes | str = b"", check: bool = True) -> Any:
    """One of TOOLS, run with fixed arguments; text in, text out when ``data`` is text."""
    return subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [str(TOOLS[name]), *map(str, args)],
        input=data,
        capture_output=True,
        text=isinstance(data, str),
        timeout=120,
        check=check,
    )


def run(*args: str | Path, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return tool("bash", SCRIPT, *args, data=stdin, check=False)


def new_key(path: Path) -> tuple[str, str]:
    """An age key pair: (its public key, its secret key line)."""
    tool("age-keygen", "-o", path)
    secret = next(line for line in path.read_text().splitlines() if line.startswith("AGE-"))
    return tool("age-keygen", "-y", path, data="").stdout.strip(), secret


def encrypt(data: bytes, recipient: str, target: Path) -> None:
    tool("age", "--encrypt", "--recipient", recipient, "-o", target, data=data)


def archive(root: Path) -> bytes:
    return tool("tar", "--create", "--file=-", "-C", root, "etc").stdout


@pytest.fixture
def backups(engine: Engine, tmp_path: Path) -> Iterator[tuple[Path, str, str]]:  # noqa: F811
    """A folder with one night's pair, as the server keeps it; the key's public and secret
    parts."""
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM monitor_state"))
        for key in ("restore-test-1", "restore-test-2"):
            connection.execute(
                text("INSERT INTO monitor_state (key, value, updated_at) VALUES (:k, '{}', now())"),
                {"k": key},
            )
    url = TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    dump = tool("pg_dump", "--format=custom", url, check=False)
    if dump.returncode != 0:  # pg_dump older than the test database's PostgreSQL
        pytest.skip(f"pg_dump cannot dump the test database: {dump.stderr.decode()[:120]}")

    folder = tmp_path / "backups"
    folder.mkdir()
    public, secret = new_key(tmp_path / "key.txt")
    encrypt(dump.stdout, public, folder / f"agent-gateway-db-{STAMP}.dump.age")
    root = tmp_path / "root"
    for name in (
        "etc/dmd-gateway/gateway.env",
        "etc/dmd-signer/signer.env",
        "etc/dmd-signer/issuer-key.pem",
        "etc/debitmydata/debitmydata.conf",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("not a real secret\n")
    encrypt(archive(root), public, folder / f"agent-gateway-config-{STAMP}.tar.age")
    # An older night, which the folder form passes over.
    encrypt(b"older", public, folder / "agent-gateway-db-20261115T031500Z.dump.age")
    yield folder, public, secret
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM monitor_state"))


def test_the_newest_pair_is_restored_with_the_pasted_key(
    backups: tuple[Path, str, str],
) -> None:
    folder, public, secret = backups
    result = run(folder, stdin=secret + "\n")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert secret not in out + result.stderr
    assert f"Backup of {STAMP}" in out and f"Backup key: {public}" in out
    assert "  etc/dmd-signer/issuer-key.pem" in out
    assert "ok: the settings, the signer's settings and the issuer key are there" in out
    assert "dbname: agent_gateway_test" in out and "Archive created at" in out
    rows = {
        line.split()[0]: int(line.split()[1])
        for line in out.splitlines()
        if line.startswith("    ") and len(line.split()) == 2 and line.split()[1].isdigit()
    }
    assert rows["monitor_state"] == 2 and rows["identities"] == 0
    assert f"RESTORE TEST PASSED: both files of {STAMP}" in out
    assert "# backup: 0" in out
    # Nothing decrypted stays behind: the key and the lists were in a private folder, gone.
    assert sorted(path.name for path in folder.iterdir()) == [
        f"agent-gateway-config-{STAMP}.tar.age",
        "agent-gateway-db-20261115T031500Z.dump.age",
        f"agent-gateway-db-{STAMP}.dump.age",
    ]


def test_a_key_file_and_two_files_work_too(backups: tuple[Path, str, str], tmp_path: Path) -> None:
    folder, _, secret = backups
    key = tmp_path / "pasted.txt"
    key.write_text(secret + "\n")
    db = folder / f"agent-gateway-db-{STAMP}.dump.age"
    config = folder / f"agent-gateway-config-{STAMP}.tar.age"
    result = run("--key-file", key, db, config)
    assert result.returncode == 0, result.stderr
    assert "RESTORE TEST PASSED" in result.stdout


def test_what_cannot_be_restored_is_said(backups: tuple[Path, str, str], tmp_path: Path) -> None:
    folder, public, _ = backups
    _, other = new_key(tmp_path / "other.txt")
    wrong = run(folder, stdin=other + "\n")
    assert wrong.returncode == 1
    assert "FAILED: the settings archive cannot be read" in wrong.stderr
    assert "RESTORE TEST PASSED" not in wrong.stdout

    not_a_key = run(folder, stdin="my password\n")
    assert not_a_key.returncode == 1 and "not an age secret key" in not_a_key.stderr

    older = folder / "agent-gateway-db-20261115T031500Z.dump.age"
    mismatched = run(older, folder / f"agent-gateway-config-{STAMP}.tar.age", stdin="x\n")
    assert mismatched.returncode == 1 and "from different nights" in mismatched.stderr

    # A settings archive without the issuer key: what a new server would miss is named.
    root = tmp_path / "partial"
    (root / "etc/dmd-gateway").mkdir(parents=True)
    (root / "etc/dmd-gateway/gateway.env").write_text("x\n")
    encrypt(archive(root), public, folder / f"agent-gateway-config-{STAMP}.tar.age")
    _, _, secret = backups
    partial = run(folder, stdin=secret + "\n")
    assert partial.returncode == 1
    assert "MISSING: /etc/dmd-signer/issuer-key.pem" in partial.stderr
    assert "lacks what a new server needs" in partial.stderr

    assert run().returncode == 2  # no backup named: how to use it


def test_a_real_size_dump_is_read_to_its_end(
    backups: tuple[Path, str, str],
    engine: Engine,  # noqa: F811
) -> None:
    """pg_restore --list stops reading once it has the list: the rest of the decrypted dump is
    read and dropped, so that age is not cut off mid-file (which failed every dump larger
    than a pipe's buffer, 64 KiB: production's first one was 520 KB)."""
    folder, public, secret = backups
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO monitor_state (key, value, updated_at) "
                "SELECT 'bulk-' || n, jsonb_build_object('x', "
                "(SELECT string_agg(md5(random()::text || n || k), '') "
                "FROM generate_series(1, 12) k)), now() FROM generate_series(1, 3000) n"
            )
        )
    url = TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    target = folder / f"agent-gateway-db-{STAMP}.dump.age"
    encrypt(tool("pg_dump", "--format=custom", url).stdout, public, target)
    assert target.stat().st_size > 512 * 1024

    result = run(folder, stdin=secret + "\n")
    assert result.returncode == 0, result.stderr
    assert "monitor_state" in result.stdout and " 3002\n" in result.stdout
    assert "RESTORE TEST PASSED" in result.stdout
