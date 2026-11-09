"""The "Get a DID" page: production's docs page and /get-did everywhere, in the
debitmydata.com look, locked down by its Content-Security-Policy, and never asking for a
private key."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from importlib.resources import files

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine

from dmd_gateway.api import get_did_page
from dmd_gateway.main import create_app
from dmd_gateway.settings import Settings

from .conftest import UNREACHABLE_DB
from .test_staging_tools import staging_client  # noqa: F401


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _config(page: str) -> dict:
    block = r'<script type="application/json" id="page-config">(.*?)</script>'
    return json.loads(re.search(block, page, re.S).group(1))


def test_on_production_the_docs_address_leads_to_get_a_did(client: TestClient) -> None:
    response = client.get("/get-did")
    assert response.status_code == 200
    assert "<h1>Get your <span class=accent>DebitMyData</span> DID</h1>" in response.text
    assert _config(response.text) == {"staging": False, "app": False}
    assert 'href="/docs/advanced"' in response.text  # the advanced option
    assert "Staging server." not in response.text  # the test-coin button is staging's
    assert "Guided test" not in response.text  # staging only
    for path in ("/", "/docs"):
        moved = client.get(path, follow_redirects=False)
        assert moved.status_code == 307 and moved.headers["location"] == "/get-did"


def test_on_staging_it_is_at_get_did_with_test_coins(staging_client: TestClient) -> None:  # noqa: F811
    page = staging_client.get("/get-did").text
    assert _config(page) == {"staging": True, "app": False}
    assert "Staging server." in page and 'href="/docs">Guided test' in page
    assert "Try a DID registration" in staging_client.get("/docs").text  # still the guided test


def test_only_the_page_own_script_style_and_font_may_run(client: TestClient) -> None:
    response = client.get("/get-did")
    csp = response.headers["content-security-policy"]
    script = re.search(r"<script>(.*)</script>", response.text, re.S).group(1)
    style = re.search(r"<style>(.*)</style>", response.text, re.S).group(1)
    assert f"script-src 'sha256-{_sha256(script)}';" in csp
    assert f"style-src 'sha256-{_sha256(style)}';" in csp
    for rule in (
        "default-src 'none'",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "frame-ancestors 'none'",
    ):
        assert rule in csp
    assert "unsafe" not in csp and "googleapis" not in response.text
    assert response.headers["cache-control"] == "no-store, no-transform"


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


def test_a_dmd_address_in_the_wallet_box_is_explained() -> None:
    # The page recognises every DMD network's address prefix (the payment adapter's list), so
    # someone who pastes the wallet they pay from is told what the box is for.
    from dmd_gateway.adapters.dmd_native import NETWORKS

    script = get_did_page._SCRIPT
    prefixes = re.search(r"const DMD_ADDRESS = /\^\(([a-z|]+)\)1", script).group(1).split("|")
    assert set(prefixes) == {hrp for _, hrp in NETWORKS.values()}
    assert "This is a DMD address: you don't need one here." in script
    assert "id=wallet-hint" in get_did_page.page_html(staging=False)


def test_the_page_says_the_did_needs_an_owner_not_a_particular_wallet() -> None:
    # No wallet extension is required: the owner is any Ethereum-style wallet (its address typed,
    # the signature pasted from the wallet app) or an agent's key, and the page says so.
    html = get_did_page.page_html(staging=False)
    assert "<legend>Who owns it</legend>" in html
    assert "Every DID needs an owner: a wallet or an agent's key." in html
    assert "No browser extension needed" in html
    assert "id=wallet-address-label" in html
    # the steps not open yet say when they open
    assert html.count('class="small locked"') == 3
    assert "Opens after <strong>Get my quote</strong> at step 1" in html
    script = get_did_page._SCRIPT
    assert "It needs no balance (no USDC, no ETH)" in html
    assert "Every DID needs an owner before step 2 opens: type the 0x… address" in script
    assert "No wallet extension in this browser, and you don't need one" in script
    # a wrong address says what is wrong with it
    assert "characters an Ethereum-style address can't have" in script
    assert "so it was probably mistyped" in script
    assert "Connect your wallet, or type its address" not in script


def test_every_page_for_people_has_the_same_top_bar_and_footer(
    client: TestClient,
    staging_client: TestClient,  # noqa: F811
) -> None:
    from dmd_gateway.api import brand

    pages = {
        client: ("/get-did", "/docs/advanced", "/policies", "/policies/refunds"),
        staging_client: ("/get-did", "/docs", "/docs/advanced", "/policies/terms"),
    }
    for server, paths in pages.items():
        staging = server is staging_client
        for path in paths:
            response = server.get(path)
            page = response.text
            assert '<header class="topbar' in page and '<footer class="site-footer' in page, path
            assert brand.CSS in page, path  # the shared font and colours
            assert "font-src 'self'" in response.headers["content-security-policy"], path
            assert ('href="/docs">Guided test' in page) is staging, path
            assert ('href="https://api.debitmydata.io/' in page) is staging, path  # Real API
            assert page.count("aria-current=page") >= 1, path


# --------------------------------------------------------------------------- the DMD payment link

PAY = "debitmydata:dm1q2qkgztajr4qt2pv8zgw3g2mjeqqyeqd6zca7f2?amount=125.00000000"
# The same texts drawn by an independent encoder (Nayuki's qrcodegen, level M, byte mode):
# version, mask, and the SHA-256 of its rows of modules (1 = dark, rows joined by newlines).
QR_REFERENCE = (
    ("DMD", 1, 0, "35ad08347947657d28e719f4c2f662171c38615b28fc3dff53e29ff47d18b2ee"),
    (PAY, 5, 2, "822d820fc62226c4168fcb4c5af397e9cbc9e5828902ac2e5c18321d9ceea700"),
    (
        PAY + "&label=" + "DebitMyData%20DID%20registration%20" * 3,  # version 10: version bits
        10,
        4,
        "83a63ff4299bdfbb7486da35a0a87fd83d5282e0fe1b58e7f9160942a9bc3691",
    ),
)
_DRAW = """
const qr = require(process.argv[1]);
const rows = (modules) => modules.map((row) => row.map((dark) => (dark ? "1" : "0")).join(""));
const drawn = JSON.parse(process.argv[2]).map((text) => {
  const symbol = qr.build(text);
  return [symbol.version, symbol.mask, rows(symbol.modules).join("\\n")];
});
process.stdout.write(JSON.stringify(drawn));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run the page's script")
def test_the_qr_code_is_the_one_an_independent_encoder_draws() -> None:
    encoder = str(files("dmd_gateway") / "static" / "qr.js")
    texts = [text for text, *_ in QR_REFERENCE]
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603 (fixed arguments, the package's own file)
        [node, "-e", _DRAW, encoder, json.dumps(texts)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    drawn = json.loads(result.stdout)
    for (text, version, mask, digest), (got_version, got_mask, rows) in zip(
        QR_REFERENCE, drawn, strict=True
    ):
        assert (got_version, got_mask) == (version, mask), text
        assert hashlib.sha256(rows.encode()).hexdigest() == digest, text


def test_dmd_payments_get_a_qr_code_and_a_wallet_link() -> None:
    script = get_did_page._SCRIPT
    # The encoder is part of the page's own script (no code from elsewhere), loaded first.
    assert script.index("const DmdQr") < script.index("DmdQr.svg(")
    # The link DMD wallets open: DMD Core registers "debitmydata:" and refuses other prefixes.
    assert re.findall(r"`(\w+):\$\{recipient\}", script) == ["debitmydata"]
    assert "?amount=${amount}`" in script
    assert "Open in my DMD wallet" in script and "check both" in script
    # Once a payment arrives (or the quote expires) they are removed, so nobody pays twice.
    assert 'querySelector(".pay-qr")' in script


# --------------------------------------------------------------------------- the installable app

APP_HOST = "id.debitmydata.io"


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    """The production gateway as reached at the app's own address."""
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    app = create_app(settings.model_copy(update={"app_host": APP_HOST}), environ={}, engine=engine)
    with TestClient(app, base_url=f"https://{APP_HOST}") as test_client:
        yield test_client


def test_the_app_host_serves_the_page_at_its_root_with_what_installs_it(
    app_client: TestClient,
) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    page = response.text
    assert _config(page) == {"staging": False, "app": True}
    assert response.headers["content-security-policy"] == get_did_page.CSP
    for tag in (
        '<link rel=manifest href="/manifest.webmanifest">',
        '<meta name=theme-color content="#640000">',
        '<link rel=apple-touch-icon href="/app/apple-touch-icon.png?v=2">',
        "<meta name=apple-mobile-web-app-capable content=yes>",
        '<meta name=apple-mobile-web-app-title content="DebitMyData DID">',
    ):
        assert tag in page
    # "Get a DID" in the top bar is the app's root; the old address leads there.
    assert '<a class=on aria-current=page href="/">Get a DID</a>' in page
    moved = app_client.get("/get-did", follow_redirects=False)
    assert moved.status_code == 307 and moved.headers["location"] == "/"
    # The API the page calls is the same gateway.
    assert app_client.get("/v1/payment-options?service=did_registration").status_code == 200


def test_the_manifest_makes_the_root_an_app(app_client: TestClient) -> None:
    response = app_client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["id"] == manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["name"] == "DebitMyData DID" and len(manifest["short_name"]) <= 12
    sizes = {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in manifest["icons"] + [{"src": "/app/apple-touch-icon.png"}]:
        image = app_client.get(icon["src"])
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert image.content.startswith(b"\x89PNG")
    assert app_client.get("/app/other.png").status_code == 404


def test_the_service_worker_never_touches_the_api(app_client: TestClient) -> None:
    response = app_client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    worker = response.text
    code = "\n".join(line for line in worker.splitlines() if not line.startswith("//"))
    # Only the page (network first), its font and icons are kept; nothing under /v1/.
    assert "/v1/" not in code and 'request.method !== "GET"' in code
    assert "fetch(request)" in worker and "You're offline" in worker
    for name in get_did_page.FONTS:
        assert f"/fonts/{name}" in worker
    # The page registers it only in the app.
    assert 'config.app && "serviceWorker" in navigator' in get_did_page._SCRIPT
    assert 'register("/sw.js")' in get_did_page._SCRIPT


def test_the_app_exists_only_at_its_own_address(client: TestClient, settings: Settings) -> None:
    # An installed app is tied to one address: api.debitmydata.io offers none of it.
    page = client.get("/get-did").text
    assert "rel=manifest" not in page and "apple-touch-icon" not in page
    for path in ("/manifest.webmanifest", "/sw.js", "/app/icon-192.png"):
        assert client.get(path).status_code == 404, path
    assert client.get("/", follow_redirects=False).headers["location"] == "/get-did"
    # The setting takes a host name only.
    for wrong in ("https://id.debitmydata.io", "id.debitmydata.io/app", "id debitmydata"):
        with pytest.raises(ValidationError):
            settings.model_validate({**settings.model_dump(), "app_host": wrong})
    assert (
        settings.model_validate(
            {**settings.model_dump(), "app_host": "ID.DebitMyData.io "}
        ).app_host
        == APP_HOST
    )


def test_staging_root_is_still_the_guided_test(staging_client: TestClient) -> None:  # noqa: F811
    moved = staging_client.get("/", follow_redirects=False)
    assert moved.status_code == 307 and moved.headers["location"] == "/docs"


# --------------------------------------------------------------------------- explaining DMD


def test_usdc_comes_first_and_dmd_is_explained() -> None:
    script = get_did_page._SCRIPT
    assert 'option.asset === "USDC" ? 0 : option.asset === "DMD" ? 1 : 2' in script
    assert "What is DMD, and how do I get it?" in script and 'aria-controls": "get-dmd"' in script
    html = get_did_page.page_html(staging=False)
    guide = re.search(r"<div id=get-dmd class=explain hidden>(.*?)</div>", html, re.S).group(1)
    assert "native coin of the DebitMyData Proof-of-Work chain" in guide
    assert "a payment method, not an investment" in guide
    assert "https://pool.debitmydata.io/" in guide and "https://debitmydata.io/#wallet" in guide
    assert "not listed on any exchange yet" in guide
    # Links to other sites open in a new tab, without giving them this page.
    assert guide.count("target=_blank") == guide.count("rel=noopener") == 4


# --------------------------------------------------------------------------- top bar and footer


def test_the_top_bar_folds_into_a_menu_on_phones_without_a_script(
    client: TestClient,
    staging_client: TestClient,  # noqa: F811
) -> None:
    for test_client, staging in ((client, False), (staging_client, True)):
        for path in ("/get-did", "/policies/terms", "/docs/advanced"):
            page = test_client.get(path).text
            # The DebitMyData name leads to the site's home, not to another site.
            assert '<a class=brand href="/">' in page, path
            # A checkbox and its label open the menu: no script, so every page's CSP allows it.
            assert "<input type=checkbox id=menu-toggle class=menu-toggle" in page, path
            assert "<label for=menu-toggle class=menu-button" in page, path
            assert "<nav id=site-menu class=modes" in page, path
            assert ".menu-toggle:checked~.modes{display:flex}" in page, path
            # The footer, in columns.
            for head in ("Get a DID", "Developers", "Policies", "DebitMyData", "Contact"):
                assert f"<p class=head>{head}</p>" in page, (path, head)
            for href in (
                "/policies/terms",
                "/policies/refunds",
                "https://debitmydata.com/",
                "https://pool.debitmydata.io/",
                "https://github.com/DebitMyData-PoW/agent-gateway",
                "mailto:info@debitmydata.com",
            ):
                assert f'href="{href}"' in page.split('<footer class="site-footer">')[1], href
            guided = ">Guided test</a>" in page.split("<footer")[1]
            assert guided is staging, path
            # The "back to top" button: a plain link, faded in by CSS as the page scrolls. The
            # API docs page keeps its own (placed above its panel): one button per page.
            button = '<a class=back-to-top href="#top" aria-label="Back to top"' in page
            assert button is (path != "/docs/advanced"), path
            assert page.count("<a class=back-to-top ") == (0 if path == "/docs/advanced" else 1)
            # Footer links open in a new tab; the email address is a plain mailto: link.
            footer = page.split('<footer class="site-footer">')[1]
            assert '<a href="/policies/terms" target=_blank rel=noopener>Terms</a>' in footer
            assert '<a href="mailto:info@debitmydata.com">info@debitmydata.com</a>' in footer
            # Cloudflare must not rewrite the page (its email protection would hide the
            # address behind a script the CSP refuses).
            assert "no-transform" in test_client.get(path).headers["cache-control"], path
