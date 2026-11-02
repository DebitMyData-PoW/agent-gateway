"""The guided staging page (/try): present only with the staging test tools, locked down by
its Content-Security-Policy, and linked from the docs page's step-by-step guide."""

from __future__ import annotations

import base64
import hashlib
import re

from fastapi.testclient import TestClient

from dmd_gateway.api import try_page

from .test_staging_tools import staging_client  # noqa: F401


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def test_the_page_is_served_on_staging(staging_client: TestClient) -> None:  # noqa: F811
    response = staging_client.get("/try")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "Try a DID registration" in page
    assert "value=dmd_pow_native" in page and "value=x402_usdc_base" in page
    assert "Run all steps" in page and "id=countdown" in page


def test_the_page_is_absent_without_the_staging_tools(client: TestClient) -> None:
    assert client.get("/try").status_code == 404


def test_only_the_page_own_script_and_style_may_run(staging_client: TestClient) -> None:  # noqa: F811
    response = staging_client.get("/try")
    csp = response.headers["content-security-policy"]
    script = re.search(r"<script>(.*)</script>", response.text, re.S).group(1)
    style = re.search(r"<style>(.*)</style>", response.text, re.S).group(1)
    assert f"script-src 'sha256-{_sha256(script)}'" in csp
    assert f"style-src 'sha256-{_sha256(style)}'" in csp
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_script_follows_the_documented_order_for_each_payment_option() -> None:
    script = try_page._SCRIPT
    dmd = '["key", "quote", "pay", "settle", "challenge", "sign", "proof", "issue"]'
    usdc = '["key", "quote", "challenge", "sign", "proof", "paysign", "issue"]'
    assert f"dmd_pow_native: {dmd}" in script
    assert f"x402_usdc_base: {usdc}" in script
    for path in (
        "/v1/staging/test-agent-keys",
        "/v1/dids/registration-quotes",
        "/v1/staging/test-payments",
        "/v1/staging/test-signatures",
        "/controller-challenges",
        "/controller-proofs",
        "/v1/dids/registrations",
    ):
        assert path in script
    # The private key is only ever sent to the signing tool, and never displayed.
    assert script.count("private_key: values.privateKey") == 1
    assert '"(kept in this page only)"' in script


def test_the_docs_guide_links_to_the_page(staging_client: TestClient) -> None:  # noqa: F811
    description = staging_client.get("/openapi.json").json()["info"]["description"]
    assert "[the guided test page](/try)" in description
