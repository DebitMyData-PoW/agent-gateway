"""The guided test page: on staging it is the docs page (/docs), with the Swagger UI page as
the advanced option (/docs/advanced); elsewhere /docs stays the Swagger UI page. The guided
page is locked down by its Content-Security-Policy."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from dmd_gateway.api import try_page

from .test_staging_tools import staging_client  # noqa: F401


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def test_the_guided_page_is_the_docs_page_on_staging(staging_client: TestClient) -> None:  # noqa: F811
    response = staging_client.get("/docs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "Try a DID registration" in page
    assert "value=dmd_pow_native" in page and "value=x402_usdc_base" in page
    assert "Run all steps" in page and "id=countdown" in page and "id=funding" in page
    assert 'href="/docs/advanced"' in page


def test_the_swagger_page_is_the_advanced_option_on_staging(
    staging_client: TestClient,  # noqa: F811
) -> None:
    advanced = staging_client.get("/docs/advanced")
    assert advanced.status_code == 200
    assert '"tryItOutEnabled": true' in advanced.text
    assert "/openapi.json" in advanced.text


@pytest.mark.parametrize("path", ["/", "/try"])
def test_the_old_address_and_the_root_lead_to_the_guided_page(
    staging_client: TestClient,  # noqa: F811
    path: str,
) -> None:
    response = staging_client.get(path, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_outside_staging_there_is_no_guided_test_page(client: TestClient) -> None:
    # /docs leads to the "Get a DID" page there (tests/test_get_did_page.py).
    docs = client.get("/docs", follow_redirects=False)
    assert docs.status_code == 307 and docs.headers["location"] == "/get-did"
    assert '"tryItOutEnabled": true' in client.get("/docs/advanced").text
    assert client.get("/try", follow_redirects=False).status_code == 404


def test_only_the_page_own_script_and_style_may_run(staging_client: TestClient) -> None:  # noqa: F811
    response = staging_client.get("/docs")
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
        "/v1/staging/test-wallet",
    ):
        assert path in script
    # The private key is only ever sent to the signing tool, and never displayed.
    assert script.count("private_key: values.privateKey") == 1
    assert '"(kept in this page only)"' in script


def test_the_advanced_guide_links_back_to_the_guided_page(
    staging_client: TestClient,  # noqa: F811
) -> None:
    description = staging_client.get("/openapi.json").json()["info"]["description"]
    assert "[go back to the guided test page](/docs)" in description
