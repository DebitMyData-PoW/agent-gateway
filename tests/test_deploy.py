"""The staging nginx site: valid for nginx, with the request rate limits in place; and the
setup scripts' settings file, which a new setting must never corrupt.

Needs the nginx binary (skipped without it); nothing is started, only ``nginx -t``.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
FILES = DEPLOY / "staging" / "files"
SETUP_SCRIPTS = (DEPLOY / "staging" / "setup.sh", DEPLOY / "production" / "setup.sh")
BASH = shutil.which("bash")
SITE = (FILES / "nginx-site.conf").read_text(encoding="utf-8")
NGINX = shutil.which("nginx")


def _certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "staging.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "origin.pem", directory / "origin.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.mark.skipif(NGINX is None, reason="nginx is not installed")
@pytest.mark.parametrize("app_host", ["", " id.staging.test"], ids=["api", "api-and-app"])
def test_nginx_accepts_the_staging_site(tmp_path: Path, app_host: str) -> None:
    cert, key = _certificate(tmp_path)
    (tmp_path / "logs").mkdir()
    shutil.copy(FILES / "nginx-proxy.conf", tmp_path / "dmd-gateway-proxy.conf")
    site = (
        SITE.replace("@HOSTNAME@", "staging.test")
        .replace("@APP_HOSTNAME@", app_host)
        .replace("/etc/ssl/cloudflare/origin.pem", str(cert))
        .replace("/etc/ssl/cloudflare/origin.key", str(key))
        .replace("/etc/nginx/snippets/", f"{tmp_path}/")
        .replace("/var/log/nginx/", f"{tmp_path}/logs/")
        .replace("listen 443", "listen 127.0.0.1:18443")
    )
    site = "\n".join(line for line in site.splitlines() if "listen [::]" not in line)
    (tmp_path / "site.conf").write_text(site, encoding="utf-8")
    (tmp_path / "nginx.conf").write_text(
        f"pid {tmp_path}/nginx.pid;\n"
        f"error_log {tmp_path}/logs/error.log;\n"
        "events {}\n"
        f"http {{ access_log off; include {tmp_path}/site.conf; }}\n",
        encoding="utf-8",
    )
    assert NGINX is not None
    result = subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [NGINX, "-t", "-q", "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_every_location_is_rate_limited() -> None:
    locations = SITE.split("location ")[1:]
    proxied = [block for block in locations if "dmd-gateway-proxy.conf" in block]
    # the API, MCP (at /agenticsuperhighway and at /mcp, its first address) and the staging
    # test tools
    assert len(proxied) == 4
    for block in proxied:
        assert "limit_req zone=" in block
    mcp = [block for block in proxied if block.startswith(("/agenticsuperhighway ", "/mcp "))]
    assert len(mcp) == 2 and all("zone=dmd_mcp burst=" in block for block in mcp)
    assert "zone=dmd_writes burst=" in SITE
    assert "zone=dmd_test_tools burst=" in SITE
    assert "limit_conn dmd_connections" in SITE
    # Refusals are JSON, like every other error of the gateway.
    assert "error_page 429 = @rate_limited;" in SITE
    assert '"code":"rate_limited"' in SITE


def test_production_serves_mcp_at_both_addresses_with_its_limits() -> None:
    site = (DEPLOY / "production" / "files" / "nginx-site.conf").read_text(encoding="utf-8")
    blocks = site.split("location ")[1:]
    mcp = [block for block in blocks if block.startswith(("/agenticsuperhighway ", "/mcp "))]
    assert len(mcp) == 2 and all("zone=dmd_mcp burst=" in block for block in mcp)


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
@pytest.mark.parametrize(
    ("script", "settings", "other"),
    [
        ("staging", "DMD_GW_ENVIRONMENT=production", "production"),
        ("staging", "DMD_GW_ENVIRONMENT='production'", "production"),
        ("production", "DMD_GW_ENVIRONMENT=staging", "staging"),
        ("production", "DMD_GW_ENVIRONMENT=development", "staging"),
    ],
)
def test_an_update_never_runs_on_the_other_server(
    script: str, settings: str, other: str, tmp_path: Path
) -> None:
    """Staging's update on the production server would add staging's test settings to
    production's settings file (and the other way round): each one stops before changing
    anything, even before pausing the monitor."""
    env_file = tmp_path / "gateway.env"
    env_file.write_text(f"{settings}\nDMD_GW_LOG_LEVEL=INFO\n", encoding="utf-8")
    calls = tmp_path / "calls"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "$@" >>"{calls}"\n', encoding="utf-8")
    systemctl.chmod(0o755)
    assert BASH is not None
    result = subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [BASH, str(DEPLOY / script / "update.sh")],
        env={
            "PATH": "/usr/bin:/bin",
            "DMD_GW_ENV_FILE": str(env_file),
            "SYSTEMCTL": str(systemctl),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Nothing was changed" in result.stderr
    assert f"deploy/{other}/update.sh" in result.stderr
    assert not calls.exists()  # the monitor was not even paused


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_staging_setup_refuses_production_settings(tmp_path: Path) -> None:
    """The staging setup run by hand on the production server stops at its first check."""
    lines = (DEPLOY / "staging" / "setup.sh").read_text(encoding="utf-8").splitlines()
    start = next(n for n, line in enumerate(lines) if "Never on the production server" in line)
    end = next(n for n in range(start, len(lines)) if lines[n] == "fi")
    first_change = next(n for n, line in enumerate(lines) if line.startswith("chmod "))
    assert end < first_change  # before anything on the server is changed
    helpers = [line for line in lines if line.startswith(("fail()", "env_value()"))]
    guard = "\n".join(lines[start : end + 1])
    env_file = tmp_path / "gateway.env"
    assert BASH is not None
    for value, refused in (("production", True), ("'production'", True), ("staging", False)):
        env_file.write_text(f"DMD_GW_ENVIRONMENT={value}\n", encoding="utf-8")
        result = subprocess.run(  # noqa: S603 (fixed arguments, test files only)
            [BASH, "-c", 'ENV_FILE="$1"\n' + "\n".join(helpers) + "\n" + guard, "_", str(env_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 1) is refused, value
        assert ("Nothing was changed" in result.stderr) is refused


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
@pytest.mark.parametrize("script", SETUP_SCRIPTS, ids=["staging", "production"])
def test_a_new_setting_never_lands_on_the_end_of_the_last_line(
    script: Path, tmp_path: Path
) -> None:
    """A settings file edited by hand may lack its final line break: every append adds it
    first, so that the new setting is a line of its own."""
    lines = script.read_text(encoding="utf-8").splitlines()
    helpers = [line for line in lines if line.startswith(("end_line()", "add_setting()"))]
    for number, line in enumerate(lines):
        if '>>"$ENV_FILE"' not in line or line in helpers:
            continue
        assert lines[number - 1].strip() == "end_line", f"{script}:{number + 1}: {line}"
    assert any(line.startswith("end_line()") for line in helpers)
    if any(line.startswith("add_setting()") for line in helpers):
        assert "end_line &&" in next(h for h in helpers if h.startswith("add_setting()"))

    settings = tmp_path / "gateway.env"
    calls = "end_line; printf 'B=1\\n' >>\"$ENV_FILE\""
    if len(helpers) == 2:
        calls += "; add_setting C 2; add_setting C 3"  # once only, never replaced
    added = "B=1\nC=2\n" if len(helpers) == 2 else "B=1\n"
    for before in ("A='https://x'", "A='https://x'\n", ""):
        settings.write_text(before, encoding="utf-8")
        assert BASH is not None
        subprocess.run(  # noqa: S603 (fixed arguments, test files only)
            [BASH, "-c", 'ENV_FILE="$1"\n' + "\n".join(helpers) + "\n" + calls, "_", str(settings)],
            check=True,
        )
        expected = (before.rstrip("\n") + "\n" if before else "") + added
        assert settings.read_text(encoding="utf-8") == expected


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
@pytest.mark.parametrize("script", SETUP_SCRIPTS, ids=["staging", "production"])
def test_nginx_also_answers_for_the_app_host_when_one_is_set(script: Path, tmp_path: Path) -> None:
    """The Get a DID app's host (DMD_GW_APP_HOST) joins the API's in nginx's server_name."""
    lines = script.read_text(encoding="utf-8").splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith('APP_HOST="$(env_value'))
    end = next(n for n in range(start, len(lines)) if "sites-available/dmd-gateway" in lines[n])
    render = "\n".join(lines[start : end + 1]).replace(
        "/etc/nginx/sites-available/dmd-gateway", '"$OUT"'
    )
    env_value = next(line for line in lines if line.startswith("env_value()"))
    files = script.parent / "files"
    settings, out = tmp_path / "gateway.env", tmp_path / "site.conf"
    program = "\n".join(
        (
            'ENV_FILE="$1"; OUT="$2"; FILES="$3"; HOSTNAME_PUBLIC=api.test',
            'fail() { echo "$*" >&2; exit 1; }',
            env_value,
            render,
        )
    )
    for setting, server_name in (
        ("", "server_name api.test;"),
        ("DMD_GW_APP_HOST=\n", "server_name api.test;"),
        ("DMD_GW_APP_HOST='ID.Example.test'\n", "server_name api.test id.example.test;"),
    ):
        settings.write_text("DMD_GW_ENVIRONMENT=staging\n" + setting, encoding="utf-8")
        assert BASH is not None
        subprocess.run(  # noqa: S603 (fixed arguments, test files only)
            [BASH, "-c", program, "_", str(settings), str(out), str(files)], check=True
        )
        site = out.read_text(encoding="utf-8")
        assert server_name in site and "@APP_HOSTNAME@" not in site
    settings.write_text("DMD_GW_APP_HOST=https://id.example.test/\n", encoding="utf-8")
    refused = subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [BASH, "-c", program, "_", str(settings), str(out), str(files)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 1 and "must be a host name only" in refused.stderr


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_the_staging_test_chain_never_looks_out_of_date(tmp_path: Path) -> None:
    """A regtest node restarted days after its last block must not refuse DMD quotes."""
    setup = (DEPLOY / "staging" / "setup.sh").read_text(encoding="utf-8")
    assert setup.count("maxtipage=315360000") == 2  # new installs, and existing ones
    command = next(line.strip() for line in setup.splitlines() if "sed -i '/^\\[regtest" in line)
    conf = tmp_path / "debitmydata.conf"
    conf.write_text("regtest=1\nserver=1\n[regtest]\nrpcbind=127.0.0.1\n", encoding="utf-8")
    assert BASH is not None
    subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [BASH, "-c", command, "_"], env={"DMD_CONF": str(conf), "PATH": "/usr/bin:/bin"}, check=True
    )
    lines = conf.read_text(encoding="utf-8").splitlines()
    assert lines[lines.index("[regtest]") + 1] == "maxtipage=315360000"
    wallets = (DEPLOY / "staging" / "regtest-wallets.sh").read_text(encoding="utf-8")
    assert "initialblockdownload" in wallets and "generatetoaddress 1" in wallets
