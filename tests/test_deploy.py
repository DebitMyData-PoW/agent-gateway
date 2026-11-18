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

from dmd_gateway.oauth.clients import parse_document

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
FILES = DEPLOY / "staging" / "files"
SETUP_SCRIPTS = (DEPLOY / "staging" / "setup.sh", DEPLOY / "production" / "setup.sh")
BASH = shutil.which("bash")
JQ = shutil.which("jq")
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
@pytest.mark.parametrize(
    ("app_host", "auth_host"),
    [("", ""), (" id.staging.test", ""), (" id.staging.test", " auth.staging.test")],
    ids=["api", "api-and-app", "api-app-and-auth"],
)
def test_nginx_accepts_the_staging_site(tmp_path: Path, app_host: str, auth_host: str) -> None:
    cert, key = _certificate(tmp_path)
    (tmp_path / "logs").mkdir()
    shutil.copy(FILES / "nginx-proxy.conf", tmp_path / "dmd-gateway-proxy.conf")
    site = (
        SITE.replace("@HOSTNAME@", "staging.test")
        .replace("@APP_HOSTNAME@", app_host)
        .replace("@AUTH_HOSTNAME@", auth_host)
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
        assert "@AUTH_HOSTNAME@" not in site
    settings.write_text("DMD_GW_APP_HOST=https://id.example.test/\n", encoding="utf-8")
    refused = subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [BASH, "-c", program, "_", str(settings), str(out), str(files)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 1 and "must be a host name only" in refused.stderr


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_staging_nginx_answers_for_the_auth_host_when_one_is_set(tmp_path: Path) -> None:
    """The OAuth authorization server's host (DMD_GW_AUTH_HOST) joins staging's server_name."""
    script = SETUP_SCRIPTS[0]
    lines = script.read_text(encoding="utf-8").splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith('APP_HOST="$(env_value'))
    end = next(n for n in range(start, len(lines)) if "sites-available/dmd-gateway" in lines[n])
    render = "\n".join(lines[start : end + 1]).replace(
        "/etc/nginx/sites-available/dmd-gateway", '"$OUT"'
    )
    env_value = next(line for line in lines if line.startswith("env_value()"))
    settings, out = tmp_path / "gateway.env", tmp_path / "site.conf"
    program = "\n".join(
        (
            'ENV_FILE="$1"; OUT="$2"; FILES="$3"; HOSTNAME_PUBLIC=api.test',
            'fail() { echo "$*" >&2; exit 1; }',
            env_value,
            render,
        )
    )
    argv = [str(BASH), "-c", program, "_", str(settings), str(out), str(script.parent / "files")]
    for setting, server_name in (
        ("DMD_GW_AUTH_HOST=Auth.Example.test\n", "server_name api.test auth.example.test;"),
        (
            "DMD_GW_APP_HOST=id.example.test\nDMD_GW_AUTH_HOST=auth.example.test\n",
            "server_name api.test id.example.test auth.example.test;",
        ),
    ):
        settings.write_text("DMD_GW_ENVIRONMENT=staging\n" + setting, encoding="utf-8")
        subprocess.run(argv, check=True)  # noqa: S603 (fixed arguments, test files only)
        assert server_name in out.read_text(encoding="utf-8")
    settings.write_text("DMD_GW_AUTH_HOST=https://auth.example.test\n", encoding="utf-8")
    refused = subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        argv, capture_output=True, text=True, check=False
    )
    assert refused.returncode == 1 and "DMD_GW_AUTH_HOST must be a host name" in refused.stderr


def test_staging_setup_makes_the_token_signing_key() -> None:
    """Staging creates its OAuth token-signing key once, readable by the gateway only, and sets
    its path; production never keeps that key on the API server (the setting refuses it)."""
    staging = SETUP_SCRIPTS[0].read_text(encoding="utf-8")
    assert 'OAUTH_KEY="$KEY_DIR/oauth-key.pem"' in staging
    assert 'add_setting DMD_GW_OAUTH_KEY_PATH "$OAUTH_KEY"' in staging
    assert 'if [ ! -s "$OAUTH_KEY" ]; then' in staging
    assert (
        'runuser -u gateway -- "$APP_DIR/.venv/bin/dmd-gateway-oauth-key" "$OAUTH_KEY"' in staging
    )
    # Production: never DMD_GW_OAUTH_KEY_PATH (the settings refuse it); the signer's key instead.
    assert "DMD_GW_OAUTH_KEY_PATH" not in SETUP_SCRIPTS[1].read_text(encoding="utf-8")


def test_production_signer_holds_the_token_signing_key() -> None:
    """Production: the signer makes and keeps the token key, the API gets only its public half,
    the signer may read the DID-LLMs (to check signed requests), and it signs tokens only once
    DMD_GW_AUTH_HOST opens sign-in, which also adds that host to nginx."""
    production = SETUP_SCRIPTS[1].read_text(encoding="utf-8")
    assert 'OAUTH_KEY="$SIGNER_DIR/oauth-key.pem"' in production
    assert (
        'runuser -u dmd_signer -- "$VENV/bin/dmd-gateway-oauth-key" "$OAUTH_KEY" >/dev/null'
        in production
    )
    assert 'chown dmd_signer:dmd_signer "$OAUTH_KEY"' in production
    assert 'chmod 600 "$OAUTH_KEY"' in production
    assert '--public "$OAUTH_KEY" >"$OAUTH_PUBLIC.new"' in production
    assert "printf 'DMD_GW_OAUTH_PUBLIC_KEY_PATH=%s\\n' \"$OAUTH_PUBLIC\"" in production
    assert "DMD_SIGNER_OAUTH_KEY_PATH=$OAUTH_KEY" in production
    assert 'if [ -n "$AUTH_HOST" ]; then\n    printf \'DMD_SIGNER_OAUTH_ISSUER=' in production
    # ... and the owner changes its owners signed, to check who owns it (signer/owners.py).
    assert "GRANT SELECT ON identities, owner_changes TO dmd_signer;" in production
    assert '-e "s#@AUTH_HOSTNAME@#${AUTH_HOST:+ $AUTH_HOST}#g"' in production
    # The key is made before the API starts, which loads its public half.
    assert production.index('--public "$OAUTH_KEY"') < production.index("restart dmd-gateway-api")


def _retired_keys_step() -> str:
    production = SETUP_SCRIPTS[1].read_text(encoding="utf-8")
    start = production.index("# Earlier issuer keys, after the key was replaced")
    end = production.index('chown root:dmd_signer "$SIGNER_ENV"', start)
    return production[start:end]


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
@pytest.mark.parametrize(
    ("value", "written", "failure"),
    [
        ("", None, None),
        ("z6Mk" + "a" * 44, "z6Mk" + "a" * 44, None),
        (f"'z6Mk{'a' * 44}, z6Mk{'b' * 44}'", f"z6Mk{'a' * 44},z6Mk{'b' * 44}", None),
        ("z6Mknope", None, "must be issuer public keys"),
        (f"z6Mk{'a' * 44},z6Mk{'c' * 44}", None, "never signs again"),  # the current key
    ],
)
def test_the_signer_gets_the_retired_issuer_keys(
    tmp_path: Path, value: str, written: str | None, failure: str | None
) -> None:
    """Production: DMD_GW_ISSUER_RETIRED_KEYS (gateway settings) is passed to the signer, which
    still trusts what those keys signed; the key in use is never one of them."""
    signer_env = tmp_path / "signer.env"
    signer_env.write_text("DMD_SIGNER_ISSUER_DID=did:web:api.debitmydata.io\n")
    script = "\n".join(
        [
            'fail() { echo "FAIL: $*"; exit 1; }',
            f"env_value() {{ printf '%s' {value!r}; }}" if value else "env_value() { :; }",
            f"PUBLIC_KEY=z6Mk{'c' * 44}",
            "ISSUER_KEY=/etc/dmd-signer/issuer-key.pem",
            f"SIGNER_ENV={signer_env}",
            _retired_keys_step(),
        ]
    )
    result = subprocess.run(  # noqa: S603
        [str(BASH), "-c", script], capture_output=True, text=True, check=False
    )
    if failure:
        assert result.returncode == 1 and failure in result.stdout
        return
    assert result.returncode == 0, result.stdout + result.stderr
    lines = signer_env.read_text().splitlines()
    retired = [line for line in lines if line.startswith("DMD_SIGNER_ISSUER_RETIRED_KEYS=")]
    assert retired == ([f"DMD_SIGNER_ISSUER_RETIRED_KEYS={written}"] if written else [])


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


@pytest.mark.parametrize("script", SETUP_SCRIPTS)
def test_every_update_ends_with_the_handshake_check(script: Path) -> None:
    """The discovery runbook's Day 5 gate: an update whose gateway agents cannot find or use
    fails, after nginx answers and before it says it is done."""
    setup = script.read_text(encoding="utf-8")
    command = '/bin/dmd-gateway-handshake" --local "https://$HOSTNAME_PUBLIC" ||'
    assert command in setup
    assert setup.index('step "Checking the gateway through nginx"') < setup.index(command)
    assert setup.index(command) < setup.index("printf '\\nDone.")
    assert 'fail "agents may not find or use the gateway' in setup


def test_the_hosted_client_documents_are_what_the_gateway_accepts() -> None:
    """Each document staging hosts for an app (files/oauth-clients/) reads, once installed, as
    the gateway needs it: its client_id its own address, a name, a return address; served from
    the folder setup.sh fills, rate limited like any read."""
    documents = sorted((FILES / "oauth-clients").glob("*.json"))
    assert documents
    for document in documents:
        url = f"https://staging.test/oauth/clients/{document.name}"
        text = document.read_text(encoding="utf-8").replace("@HOSTNAME@", "staging.test")
        client = parse_document(url, text.encode())
        assert client.host == "staging.test" and client.redirect_uris
    block = next(b for b in SITE.split("location ")[1:] if b.startswith("/oauth/clients/ "))
    assert "alias /var/www/dmd-gateway-clients/;" in block
    assert "limit_req zone=dmd_reads" in block


@pytest.mark.skipif(BASH is None or JQ is None, reason="bash and jq are needed")
def test_staging_setup_installs_the_client_documents_and_refuses_a_wrong_one(
    tmp_path: Path,
) -> None:
    script = SETUP_SCRIPTS[0]
    lines = script.read_text(encoding="utf-8").splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith("CLIENTS_DIR="))
    end = next(n for n in range(start, len(lines)) if lines[n] == "done")
    block = "\n".join(lines[start : end + 1]).replace(
        "CLIENTS_DIR=/var/www/dmd-gateway-clients", 'CLIENTS_DIR="$OUT"'
    )
    out, files = tmp_path / "clients", tmp_path / "files"
    shutil.copytree(FILES / "oauth-clients", files / "oauth-clients")
    (tmp_path / "clients").mkdir()
    (out / "removed-app.json").write_text("{}", encoding="utf-8")  # no longer in the repository
    program = "\n".join(
        (
            'OUT="$1"; FILES="$2"; HOSTNAME_PUBLIC=api.test',
            'fail() { echo "$*" >&2; exit 1; }',
            block,
        )
    )
    argv = [str(BASH), "-c", program, "_", str(out), str(files)]
    subprocess.run(argv, check=True)  # noqa: S603 (fixed arguments, test files only)
    installed = sorted(p.name for p in out.iterdir())
    assert installed == sorted(p.name for p in (FILES / "oauth-clients").glob("*.json"))
    grok = (out / "grok-build-staging.json").read_text(encoding="utf-8")
    assert '"https://api.test/oauth/clients/grok-build-staging.json"' in grok
    (files / "oauth-clients" / "copied.json").write_text(
        '{"client_id": "https://api.test/oauth/clients/other.json"}', encoding="utf-8"
    )
    refused = subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        argv, capture_output=True, text=True, check=False
    )
    assert refused.returncode == 1 and "its client_id must be" in refused.stderr


def _capacity(tmp_path: Path, cores: int, settings: str = "", connections: int = 100) -> str:
    """capacity.sh with this many cores, these settings and PostgreSQL's max_connections: what
    it prints, then the commands it ran (psql and systemctl, faked)."""
    calls = tmp_path / "calls"
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    state = tmp_path / "connections"
    state.write_text(str(connections), encoding="utf-8")
    fakes = {
        "nproc": f"echo {cores}",
        # runuser -u postgres -- psql ...: SHOW answers the stored value, ALTER stores one
        "runuser": (
            f'shift 3; echo "$@" >>"{calls}"\n'
            f'case "$*" in *SHOW*) cat "{state}";;\n'
            f'  *ALTER*) echo "$*" | tr -dc "0-9" >"{state}";; esac'
        ),
        "systemctl": f'echo "systemctl $*" >>"{calls}"',
    }
    for name, body in fakes.items():
        stub = stubs / name
        stub.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    env_file = tmp_path / "gateway.env"
    env_file.write_text(settings, encoding="utf-8")
    assert BASH is not None
    result = subprocess.run(  # noqa: S603 (fixed arguments, test files only)
        [
            BASH,
            "-c",
            f'fail() {{ echo "FAIL: $*"; exit 1; }}; . "{FILES / "capacity.sh"}"; '
            'echo "api=$API_WORKERS payment=$PAYMENT_WORKERS"',
        ],
        env={
            "PATH": f"{stubs}:/usr/bin:/bin",
            "ENV_FILE": str(env_file),
            "SYSTEMCTL": str(stubs / "systemctl"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    ran = calls.read_text(encoding="utf-8") if calls.exists() else ""
    calls.unlink(missing_ok=True)
    return result.stdout + ran


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_every_core_is_used_with_enough_database_connections(tmp_path: Path) -> None:
    """One API process per core (2 to 16), one payment worker per core (2 to 8) unless the
    settings say how many, and PostgreSQL raised to enough connections for all of them (and
    restarted) only when it has fewer."""
    small = _capacity(tmp_path, 1)
    assert "api=2 payment=2" in small and "ALTER" not in small  # 2*10 + 2*3 + 40 < 100

    eight = _capacity(tmp_path, 8)
    assert "api=8 payment=8" in eight
    assert "ALTER SYSTEM SET max_connections = 144" in eight  # 8*10 + 8*3 + 40
    assert "systemctl restart postgresql" in eight
    assert "144 database connections at most" in eight

    big = _capacity(tmp_path, 64, connections=500)
    assert "api=16 payment=8" in big and "ALTER" not in big and "restart" not in big

    chosen = _capacity(tmp_path, 4, "DMD_GW_PAYMENT_WORKERS=12\nDMD_GW_DATABASE_POOL_SIZE=8\n")
    assert "api=4 payment=12" in chosen
    assert "max_connections = 128" in chosen  # 4 * (8 + 5) + 12 * 3 + 40
    assert "FAIL" in _capacity(tmp_path, 4, "DMD_GW_PAYMENT_WORKERS=40\n")


def test_the_worker_unit_runs_the_payment_workers_setup_chose() -> None:
    unit = (FILES / "dmd-gateway-worker.service").read_text(encoding="utf-8")
    assert "dmd-gateway-worker --processes @PAYMENT_WORKERS@" in unit
    for script in SETUP_SCRIPTS:
        text = script.read_text(encoding="utf-8")
        assert "capacity.sh" in text and "s#@PAYMENT_WORKERS@#$PAYMENT_WORKERS#g" in text
        assert "-gt 4 ]" not in text  # no longer at most 4 API processes
