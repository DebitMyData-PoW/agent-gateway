"""The "Get a DID" page: production's docs page and /get-did everywhere, in the
debitmydata.com look, locked down by its Content-Security-Policy, and never asking for a
private key."""

from __future__ import annotations

import base64
import hashlib
import json
import re

from fastapi.testclient import TestClient

from dmd_gateway.api import get_did_page

from .test_staging_tools import staging_client  # noqa: F401


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _config(page: str) -> dict:
    block = r'<script type="application/json" id="page-config">(.*?)</script>'
    return json.loads(re.search(block, page, re.S).group(1))


def test_on_production_the_docs_page_is_get_a_did(client: TestClient) -> None:
    for path in ("/docs", "/get-did"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Get your DebitMyData DID" in response.text
        assert _config(response.text) == {"staging": False}
        assert 'href="/docs/advanced"' in response.text  # the advanced option
        assert "Staging server." not in response.text  # the test-coin button is staging's
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307 and root.headers["location"] == "/docs"


def test_on_staging_it_is_at_get_did_with_test_coins(staging_client: TestClient) -> None:  # noqa: F811
    page = staging_client.get("/get-did").text
    assert _config(page) == {"staging": True}
    assert "Staging server." in page and 'href="/docs">Guided test' in page
    assert "Try a DID registration" in staging_client.get("/docs").text  # still the guided test


def test_only_the_page_own_script_style_and_font_may_run(client: TestClient) -> None:
    response = client.get("/docs")
    csp = response.headers["content-security-policy"]
    script = re.search(r"<script>(.*)</script>", response.text, re.S).group(1)
    style = re.search(r"<style>(.*)</style>", response.text, re.S).group(1)
    assert f"script-src 'sha256-{_sha256(script)}';" in csp
    assert f"style-src 'sha256-{_sha256(style)}';" in csp
    for rule in (
        "default-src 'none'",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
    ):
        assert rule in csp
    assert "unsafe" not in csp and "googleapis" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_the_font_is_served_here(client: TestClient) -> None:
    for name in get_did_page.FONTS:
        font = client.get(f"/fonts/{name}")
        assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"
        assert font.content.startswith(b"wOF2")
        assert f"/fonts/{name}" in get_did_page._STYLE
    assert client.get("/fonts/other.woff2").status_code == 404
    assert client.get("/fonts/..%2fget-did.js").status_code == 404


def test_the_page_never_asks_for_a_private_key_and_never_pays() -> None:
    script = get_did_page._SCRIPT
    for never in (
        "private_key",
        "privateKey",
        "seed",
        "mnemonic",
        "eth_sendTransaction",
        "eth_signTypedData",
        "sendBeacon",
        "document.cookie",
    ):
        assert never not in script
    # It signs one message with the visitor's wallet, found through EIP-6963.
    assert "personal_sign" in script and "eip6963:requestProvider" in script
    # The test coins exist on staging only, and the page reads that from its settings.
    assert "/v1/staging/test-payments" in script and "config.staging" in script


def test_what_the_did_is_for_covers_every_participant_type() -> None:
    from dmd_gateway.services.quotes import ParticipantType

    assert {value for value, _ in get_did_page.PARTICIPANTS} == {t.value for t in ParticipantType}
