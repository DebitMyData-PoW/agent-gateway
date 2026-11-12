"""The "Get a DID-LLM" page: production's docs page and /get-did everywhere, in the
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

from dmd_gateway.api import brand, get_did_page
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
    assert "<h1>Get your <span class=accent>DebitMyData</span> DID-LLM</h1>" in response.text
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
    assert "Try a DID-LLM registration" in staging_client.get("/docs").text  # still the guided test


def test_only_the_page_own_script_style_and_font_may_run(client: TestClient) -> None:
    response = client.get("/get-did")
    csp = response.headers["content-security-policy"]
    script = re.search(r"<script>(.*)</script>", response.text, re.S).group(1)
    style = re.search(r"<style>(.*)</style>", response.text, re.S).group(1)
    assert f"script-src 'sha256-{_sha256(script)}' {brand.THEME_SCRIPT_SOURCE};" in csp
    assert brand.THEME_SCRIPT in response.text  # the light / dark theme, in the <head>
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
    assert "Every DID-LLM needs an owner: a wallet or an agent's key." in html
    assert "No browser extension needed" in html
    assert "id=wallet-address-label" in html
    # the steps not open yet say when they open
    assert html.count('class="small locked"') == 3
    assert "Opens after <strong>Get my quote</strong> at step 1" in html
    script = get_did_page._SCRIPT
    assert "It needs no balance (no USDC, no ETH)" in html
    assert "Every DID-LLM needs an owner before step 2 opens: type the 0x… address" in script
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
            assert f'<a class=brand href="/">{brand.LOGO}DebitMyData</a>' in page, path
            assert "font-src 'self'" in response.headers["content-security-policy"], path
            assert ('href="/docs">Guided test' in page) is staging, path
            # On staging, "Real site": the same page in production (Get a DID: the app).
            assert ("class=real href=" in page) is staging, path
            if staging:
                real = page.split("class=real href=")[1].split('"')[1]
                expected = {
                    "/get-did": "https://id.debitmydata.io/",
                    "/docs": "https://id.debitmydata.io/",
                    "/docs/advanced": "https://id.debitmydata.io/docs/advanced",
                    "/policies/terms": "https://id.debitmydata.io/policies",
                }[path]
                assert real == expected, path
            assert page.count("aria-current=page") >= 1, path
        # The DebitMyData logo in the top bar, wider than high (151 x 120).
        logo = server.get("/logo.png")
        assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
        assert _png_size(logo.content) == (151, 120)


def _png_size(png: bytes) -> tuple[int, int]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png[12:16] == b"IHDR"
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


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
    assert 'querySelectorAll(".pay-qr, .pay-card")' in script


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
        '<link rel=apple-touch-icon href="/app/apple-touch-icon.png?v=3">',
        "<meta name=apple-mobile-web-app-capable content=yes>",
        '<meta name=apple-mobile-web-app-title content="DID-LLM DebitMyData">',
    ):
        assert tag in page
    # "Get a DID" in the top bar is the app's root; the old address leads there.
    assert '<a class=on aria-current=page href="/">Get a DID-LLM</a>' in page
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
    # "DID" first: a home screen that shortens the name still shows what the app is for.
    assert manifest["name"] == manifest["short_name"] == "DID-LLM DebitMyData"
    sizes = {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in manifest["icons"] + [{"src": "/app/apple-touch-icon.png", "sizes": "180x180"}]:
        image = app_client.get(icon["src"])
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        # Each image is the size it is listed with.
        width, height = map(int, icon["sizes"].split("x"))
        assert _png_size(image.content) == (width, height), icon["src"]
    assert app_client.get("/app/other.png").status_code == 404
    # The app itself, so that the page can ask whether it is installed.
    assert manifest["related_applications"] == [
        {"platform": "webapp", "url": "/manifest.webmanifest", "id": f"https://{APP_HOST}/"}
    ]
    assert manifest["prefer_related_applications"] is False


def test_the_app_offers_itself_until_it_is_installed(app_client: TestClient) -> None:
    from dmd_gateway.api import brand

    page = app_client.get("/").text
    # A window to install it, closed until the page's script decides to open it.
    assert get_did_page.INSTALL_DIALOG in page and "<dialog id=install class=install" in page
    assert '<img src="/app/icon-512.png?v=3"' in page  # the app's own icon
    script = get_did_page._SCRIPT
    for needed in (
        'matchMedia("(display-mode: standalone)")',  # never inside the app
        "navigator.getInstalledRelatedApps()",  # nor when the browser says it is installed
        '"beforeinstallprompt"',  # the browser's install dialog, from our button
        '"appinstalled"',
        "showModal()",  # centred, over the page
    ):
        assert needed in script, needed
    assert "Add to Home Screen" in page  # iPhone: the steps by hand
    # Only "Not now" is remembered, in this browser (no cookie: nothing goes to the server),
    # for 30 days; whether the app is installed is asked again at every visit. The cross, a
    # tap outside and Back only close it.
    assert "const DECLINE_DAYS = 30;" in script
    offer = script.split("function offerTheApp()")[1]
    assert offer.count("localStorage.setItem(DECLINED") == 1 and "document.cookie" not in script
    remembered = offer.split('later.addEventListener("click"')[1].split("});")[0]
    assert "localStorage.setItem(DECLINED" in remembered
    assert '$("#install-close").addEventListener("click", close);' in offer
    # The footer's "Get the app" button opens it again, forgetting an earlier "Not now", and
    # says so when the app is already installed.
    assert brand.GET_THE_APP_BUTTON in page
    assert "#get-the-app" in offer and "localStorage.removeItem(DECLINED)" in offer
    assert "The app is already installed" in offer


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
    # The files it keeps, fetched past the browser's own cache, in a cache named after their
    # content: a new icon reaches people who already opened the page.
    assert "@CACHE@" not in worker and "@FILES@" not in worker
    assert re.search(r'const CACHE = "dmd-did-app-[0-9a-f]{12}";', worker)
    assert 'cache: "reload"' in worker
    for path in get_did_page.WORKER_FILES:
        assert f'"{path}"' in worker
        assert app_client.get(path).status_code == 200, path
    # The page registers it only in the app.
    assert 'config.app && "serviceWorker" in navigator' in get_did_page._SCRIPT
    assert 'register("/sw.js")' in get_did_page._SCRIPT


def test_the_app_exists_only_at_its_own_address(client: TestClient, settings: Settings) -> None:
    # An installed app is tied to one address: api.debitmydata.io offers none of it.
    page = client.get("/get-did").text
    assert "rel=manifest" not in page and "apple-touch-icon" not in page
    assert "id=install" not in page
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
    # While the gateway doesn't offer USDC (production, until it is switched on), the page
    # shows it first anyway, greyed and not selectable, as coming soon.
    options = script.split("async function loadOptions()")[1].split("\n  }\n")[0]
    unless_offered = 'if (!open.some((option) => option.asset === "USDC")) open.unshift(USDC_SOON);'
    assert unless_offered in options
    assert "const soon = option === USDC_SOON || LATER.includes(option);" in options
    assert "const onPage = !soon &&" in options
    assert "disabled: !onPage" in options
    assert "Coming to this page soon: for now, pay with DMD." in options
    assert "What is DMD, and how do I get it?" in script and 'aria-controls": "get-dmd"' in script
    html = get_did_page.page_html(staging=False)
    guide = re.search(r"<div id=get-dmd class=explain hidden>(.*?)</div>", html, re.S).group(1)
    assert "native coin of the DebitMyData Proof-of-Work chain" in guide
    assert "a payment method, not an investment" in guide
    assert "https://pool.debitmydata.io/" in guide and "https://debitmydata.io/#wallet" in guide
    assert "not listed on any exchange yet" in guide
    # Links to other sites open in a new tab, without giving them this page.
    assert guide.count("target=_blank") == guide.count("rel=noopener") == 4


def test_card_and_paypal_show_as_coming_soon_after_the_coins() -> None:
    script = get_did_page._SCRIPT
    options = script.split("async function loadOptions()")[1].split("\n  }\n")[0]
    # Greyed after USDC and DMD, until the gateway offers them; then the real option is listed.
    assert "for (const later of LATER) if (!open.some(later.offered)) open.push(later);" in options
    later = script.split("const LATER = [")[1].split("];")[0]
    assert '"Debit or credit card"' in later and "through Stripe" in later
    assert '"PayPal"' in later
    assert later.count("Coming to this page soon.") == 2
    # Only on this page: the API keeps listing only what can be paid with now.
    assert "card" not in json.dumps(get_did_page.PARTICIPANTS)


def test_the_page_tells_ai_agents_where_to_register_by_themselves(client: TestClient) -> None:
    page = client.get("/get-did").text
    hero = page.split("<section class=hero>")[1].split("</section>")[0]
    assert "AI agent or bot?" in hero and 'href="/llms.txt"' in hero
    assert "<code>http://testserver/mcp</code>" in hero or "/mcp</code>" in hero
    # Without a script, the payment step says where agents find the options.
    assert "<noscript>" in page and "/llms.txt" in page.split("<noscript>")[1]
    assert 'href="/llms.txt" target=_blank rel=noopener>For AI agents</a>' in page


def test_llms_txt_explains_the_registration_for_agents(client: TestClient) -> None:
    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert text.startswith("# DebitMyData DID-LLM\n")
    assert "MCP server (streamable HTTP): " in text and "/mcp" in text
    assert "/v1/dids/registration-quotes" in text and "/controller-proofs" in text
    assert "## Payment options open now" in text
    assert "dmd_register_did" in text  # the MCP steps
    assert "never asks for a private key" in text
    assert "/llms.txt" not in client.get("/openapi.json").text  # not an API endpoint


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
            for head in ("Get a DID-LLM", "Developers", "Policies", "DebitMyData", "Contact"):
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
            # "Get the app": the app's page, which opens its install window.
            assert (
                '<a class=get-app href="https://id.debitmydata.io/#get-the-app" target=_blank'
                in footer
            ), path
            assert '<a href="/policies/terms" target=_blank rel=noopener>Terms</a>' in footer
            assert '<a href="mailto:info@debitmydata.com">info@debitmydata.com</a>' in footer
            # Cloudflare must not rewrite the page (its email protection would hide the
            # address behind a script the CSP refuses).
            assert "no-transform" in test_client.get(path).headers["cache-control"], path


# --------------------------------------------------------------------------- explaining DIDs


def test_the_page_explains_dids_before_the_steps(client: TestClient) -> None:
    page = client.get("/get-did").text
    # Big buttons to the steps and to the explanation; the steps come after it.
    assert '<a class="button go" href="#start">Get my DID-LLM</a>' in page
    assert '<a class="button ghost" href="#what">What is a DID-LLM?</a>' in page
    order = [
        page.index(marker)
        for marker in ("class=stats", "id=what", "id=agents", "id=how", "id=start", "id=s1")
    ]
    assert order == sorted(order)
    # The numbers, and the explanations in plain words.
    for number, label in (("$25", "paid once"), ("4", "simple steps"), ("0", "accounts")):
        assert f"<b>{number}</b><span class=label>{label}" in page
    for text in (
        "It is a Decentralized Identifier (DID), an open standard of the W3C",
        "large language\nmodels (LLMs)",
        "You hold the key",
        "AI agents and bots",
        "Prove it is yours",
    ):
        assert text in page, text
    assert '<ul class="features three">' in page  # three cards in a row on a computer
    # Icons are drawn in the page: nothing loads from elsewhere.
    assert page.count('<svg viewBox="0 0 24 24"') >= 11
    assert '<img src="http' not in page


def test_the_footer_lists_the_three_explorers(client: TestClient) -> None:
    footer = client.get("/get-did").text.split('<footer class="site-footer">')[1]
    for url, label in (
        ("https://mempool.debitmydata.io/", "Mempool explorer"),
        ("https://chain.debitmydata.io/", "Lite explorer"),
        ("https://explorer.debitmydata.io/", "Rich list &amp; API"),
    ):
        assert f'<a href="{url}" target=_blank rel=noopener>{label}</a>' in footer


def test_the_agentic_hub_button_waits_until_the_hub_saves_entries() -> None:
    script = get_did_page._SCRIPT
    assert 'const AGENTIC_HUB = "https://agenticssocialspaces.debitmydata.com/";' in script
    # Hidden for now: the hub's "Create Entity" does not save entries yet.
    assert "const SHOW_AGENTIC_HUB = false;" in script
    issued = script.split("async function register()")[1].split("\n  }\n")[0]
    assert "SHOW_AGENTIC_HUB\n          ? element(" in issued
    assert '{ class: "next" }' in issued and '"Join the Agentic Hub"' in issued
    assert 'href: AGENTIC_HUB, target: "_blank", rel: "noopener"' in issued


def test_after_a_reload_the_registration_in_progress_is_brought_into_view() -> None:
    script = get_did_page._SCRIPT
    resume = script.split("async function resume(previous, back = null) {")[1].split("\n  }\n")[0]
    assert (
        'resumeBox.scrollIntoView({ block: "center", behavior: calm ? "auto" : "smooth" });'
        in resume
    )
    assert "go.focus({ preventScroll: true });" in resume
    assert 'resumeBox.classList.add("attention");' in resume
    css = (files("dmd_gateway") / "static" / "get-did.css").read_text(encoding="utf-8")
    assert ".resume.attention{animation:resume-glow" in css
    # No glow for people who turned animations off.
    assert "@media (prefers-reduced-motion:reduce){.resume.attention{animation:none" in css


def test_step_3_helps_people_who_sign_with_a_tool() -> None:
    script = get_did_page._SCRIPT
    assert (
        '"https://github.com/DebitMyData-PoW/agent-gateway/blob/main/docs/owner-key.md"' in script
    )
    assert "says the signature was accepted, click Continue" in script
    # The tool's way is the main one (Continue); pasting is folded below it.
    proof = script.split("async function startProof() {")[1].split("\n  }\n")[0]
    assert 'element("button", { type: "button" }, "Continue")' in proof
    assert '"Or sign it with other software and paste the signature"' in proof
    assert "did-owner-key.py prove ~/did-keys ${state.registrationId}" in proof
    # Off production, the tool's command names this gateway (DMD_GATEWAY_API=...).
    assert 'const PRODUCTION_API = "https://api.debitmydata.io";' in script
    check = script.split("async function checkSignedElsewhere(button, status) {")[1].split(
        "\n  }\n"
    )[0]
    assert "/v1/dids/registrations/${state.registrationId}" in check
    assert 'result.data.next_action === "register_did"' in check and "return register();" in check
    # Pasting the message instead of its signature is explained, not sent.
    send = script.split("async function sendProof(challenge, signature, status) {")[1]
    assert (
        'signature.startsWith("DebitMyData controller proof")' in send.split("controller-proofs")[0]
    )


def test_cards_are_paid_on_stripes_page_and_come_back_here() -> None:
    script = get_did_page._SCRIPT
    assert 'const CARD = "stripe_checkout";' in script
    # Only Stripe's own payment page is ever opened: the quote's payment_url.
    assert r"const STRIPE_PAGE = /^https:\/\/checkout\.stripe\.com\//;" in script
    card = script.split("function startCardPayment(note, paid) {")[1].split("\n  }\n")[0]
    assert "STRIPE_PAGE.test(state.paymentUrl" in card
    assert "Your card details go to Stripe only" in card
    assert "4242 4242 4242 4242" in card and "config.staging" in card
    assert "test-payments" not in card  # no test coins for cards
    # The card button goes as soon as a payment is seen, so nobody pays twice.
    assert 'querySelectorAll(".pay-qr, .pay-card")' in script
    # Back from Stripe: the address is tidied, and the registration saved in this browser
    # continues by itself.
    start = script.split("// Back from the card payment page")[1]
    assert 'history.replaceState(null, "", location.pathname + location.hash)' in start
    assert "previous.paymentId === backFor" in start and "resume(previous, back)" in start
    assert "if (back) return carryOn();" in script
    # The signature formats are said once (the API's own wording is not repeated in brackets).
    assert '"The signature can be base64url, base64 or hex."' in script
    assert "challenge.signature_encoding" not in script
    # Back after paying: no second button to pay, only the wait.
    paid = card.split("if (paid) {")[1].split("\n    }\n")[0]
    assert '"Pay $' not in paid and "return checkPayment(status);" in paid


@pytest.mark.parametrize("card", [False, True])
def test_the_page_says_how_people_pay(card: bool) -> None:
    html = get_did_page.page_html(staging=True, card=card).split("<body>")[1].split("<script")[0]
    assert ("you then pay\nby card, or in DMD with any DMD wallet." in html) is card
    assert ("you then pay\nin DMD, with any DMD wallet." in html) is not card
    assert ("pay in it or on Stripe's card payment page" in html) is card
    assert ("4242 4242 4242 4242" in html) is card


def test_the_page_offers_cards_once_the_card_option_is_on() -> None:
    from .stripe_fakes import TEST_KEY, FakeStripeAdapter
    from .test_card_payments import REGTEST_PATH

    settings = Settings(
        _env_file=None, database_url=UNREACHABLE_DB, route_registry_path=REGTEST_PATH
    )
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    app = create_app(
        settings,
        environ={"DMD_RAIL_CARD_STRIPE_ENABLED": "true", "DMD_STRIPE_SECRET_KEY": TEST_KEY},
        engine=engine,
        adapters={"StripeCheckoutAdapter": FakeStripeAdapter},
    )
    with TestClient(app) as client:
        page = client.get("/get-did").text
        assert "by card, or in DMD with any DMD wallet" in page
        options = client.get("/v1/payment-options?service=did_registration").json()
        (card,) = options["supported_payment_options"]
        assert (card["id"], card["payment_protocol"], card["asset"]) == (
            "card_stripe",
            "stripe_checkout",
            "USD",
        )


def test_the_key_option_says_how_to_make_a_key() -> None:
    real = get_did_page.page_html(staging=False)
    assert f'href="{get_did_page.OWNER_KEY_GUIDE}"' in real
    assert "<code>python3 did-owner-key.py new ~/did-keys/my-agent.key</code>" in real
    assert "make a test key of its own" not in real
    # On staging: a test key of its own, and the guide's staging part.
    staging = get_did_page.page_html(staging=True)
    assert "owner-key.md#staging-the-test-server" in staging
    assert "<code>python3 did-owner-key.py new ~/did-keys/staging-test.key</code>" in staging
    assert "make a test key of its own" in staging
