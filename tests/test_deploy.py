"""The staging nginx site: valid for nginx, with the request rate limits in place.

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

FILES = Path(__file__).resolve().parents[1] / "deploy" / "staging" / "files"
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
def test_nginx_accepts_the_staging_site(tmp_path: Path) -> None:
    cert, key = _certificate(tmp_path)
    (tmp_path / "logs").mkdir()
    shutil.copy(FILES / "nginx-proxy.conf", tmp_path / "dmd-gateway-proxy.conf")
    site = (
        SITE.replace("@HOSTNAME@", "staging.test")
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
    assert len(proxied) == 3  # the API, MCP and the staging test tools
    for block in proxied:
        assert "limit_req zone=" in block
    assert "zone=dmd_writes burst=" in SITE
    assert "zone=dmd_test_tools burst=" in SITE
    assert "limit_conn dmd_connections" in SITE
    # Refusals are JSON, like every other error of the gateway.
    assert "error_page 429 = @rate_limited;" in SITE
    assert '"code":"rate_limited"' in SITE
