"""The "Manage your DID-LLM" page (/manage): owners change a DID-LLM's owners or revoke it,
signing in their own wallets or with the owner-key tool (``sign-change``, ``revoke``)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from dmd_gateway.api import brand
from dmd_gateway.main import create_app
from dmd_gateway.settings import Settings

from .conftest import TEST_DATABASE_URL, UNREACHABLE_DB
from .fakes import AGENT_CONTROLLER, AGENT_KEY
from .test_identity_status import issue
from .test_oauth_browser import gateway  # noqa: F401
from .test_owner_changes import (
    BASE,
    HOST,
    _key_file,
    _tool,
    item,
    record,
    send,
    start,
)
from .test_quotes import ready_registry  # noqa: F401
from .test_registration import clean_chain, engine  # noqa: F401
from .test_staging_tools import staging_client  # noqa: F401
from .test_wallet_registration import wallet_signature

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
SCRIPT = (files("dmd_gateway") / "static" / "manage.js").read_text(encoding="utf-8")
APP_HOST = "id.debitmydata.io"


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _config(page: str) -> dict:
    block = r'<script type="application/json" id="page-config">(.*?)</script>'
    found = re.search(block, page, re.S)
    assert found is not None
    return json.loads(found.group(1))


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    engine_ = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    app = create_app(settings.model_copy(update={"app_host": APP_HOST}), environ={}, engine=engine_)
    with TestClient(app, base_url=f"https://{APP_HOST}") as test_client:
        yield test_client


# --------------------------------------------------------------------------- the page


def test_the_page_runs_only_its_own_script_and_style(client: TestClient) -> None:
    response = client.get("/manage")
    assert response.status_code == 200
    page = response.text
    assert "<h1>Manage your <span class=accent>DID-LLM</span></h1>" in page
    csp = response.headers["content-security-policy"]
    script = re.findall(r"<script>(.*?)</script>", page, re.S)[-1]
    style = re.search(r"<style>(.*)</style>", page, re.S)
    assert style is not None and script == SCRIPT
    assert f"script-src 'sha256-{_sha256(script)}' {brand.THEME_SCRIPT_SOURCE};" in csp
    assert f"style-src 'sha256-{_sha256(style.group(1))}';" in csp
    for rule in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'"):
        assert rule in csp
    assert "unsafe" not in csp
    assert response.headers["cache-control"] == "no-cache, no-transform"
    # Marked in the top bar, and in the footer of every page for people.
    assert '<a class=on aria-current=page href="/manage">My DID-LLM</a>' in page
    for path in ("/get-did", "/docs/advanced", "/policies"):
        other = client.get(path).text
        assert '<a href="/manage">My DID-LLM</a>' in other, path
        assert ">Manage my DID-LLM</a>" in other, path


def test_the_page_never_asks_for_a_private_key_and_needs_the_owners_signatures(
    client: TestClient,
) -> None:
    page = client.get("/manage").text
    assert "never asks for a private key or a" in page and "recovery phrase" in page
    assert "Nothing" in page and "without its owners' signatures" in page
    assert "Type REVOKE to confirm" in page and "This cannot be undone." in page
    for tool in ("debitmydata_change_owner", "debitmydata_revoke_did"):
        assert tool in page
    # The script signs in the wallet (personal_sign) or leaves it to the owner-key tool.
    assert "personal_sign" in SCRIPT and "eth_sendTransaction" not in SCRIPT
    assert "did-owner-key.py sign-change ~/did-keys" in SCRIPT
    assert "did-owner-key.py revoke ~/did-keys" in SCRIPT
    assert "seed" not in SCRIPT.lower() and "privateKey" not in SCRIPT
    assert _config(page)["apiBase"] == "http://localhost:8000"
    assert _config(page)["reasonLength"] == [3, 300]


def test_on_staging_the_tool_is_told_which_gateway(staging_client: TestClient) -> None:  # noqa: F811
    page = staging_client.get("/manage").text
    settings = staging_client.app.state.settings  # type: ignore[attr-defined]
    assert _config(page)["apiBase"] == settings.public_base_url.rstrip("/")
    assert 'href="/docs">Guided test' in page
    real = page.split("class=real href=")[1].split('"')[1]
    assert real == "https://id.debitmydata.io/manage"
    # The tool's default gateway is production's: the page names any other one.
    assert 'const PRODUCTION_API = "https://api.debitmydata.io"' in SCRIPT
    assert "DMD_GATEWAY_API=${config.apiBase}" in SCRIPT


def test_people_manage_their_did_llm_on_the_apps_address(
    client: TestClient, settings: Settings, app_client: TestClient
) -> None:
    engine_ = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    api = create_app(settings.model_copy(update={"app_host": APP_HOST}), environ={}, engine=engine_)
    with TestClient(api) as at_api:
        moved = at_api.get("/manage?did=did:web:x", follow_redirects=False)
    assert moved.status_code == 308
    assert moved.headers["location"] == f"https://{APP_HOST}/manage?did=did:web:x"
    there = app_client.get("/manage")
    assert there.status_code == 200
    assert _config(there.text)["getDid"] == "/"  # the app's root is Get a DID-LLM


def test_the_get_did_page_leads_owners_here_once_issued() -> None:
    script = (files("dmd_gateway") / "static" / "get-did.js").read_text(encoding="utf-8")
    assert 'const MANAGE = "/manage";' in script
    assert "${MANAGE}?did=${encodeURIComponent(issued.did)}" in script


# --------------------------------------------------------------------------- the tool


@needs_db
def test_the_tool_signs_a_change_made_on_the_page(
    gateway: TestClient,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The page makes the change; the key approves with ``sign-change``; the new wallet
    accepts on the page."""
    did = issued_did = issue(gateway)["did"]
    keys = tmp_path / "did-keys"
    keys.mkdir()
    _key_file(keys, "my-agent.key", AGENT_KEY)
    cli = _tool(gateway)
    wallet = Account.create()
    account = f"eip155:8453:{wallet.address}"
    change = start(gateway, did, new_controller_wallet_address=wallet.address)
    change_id = change["change_id"]

    for bad, reason in (
        ("dmd_owner_change_nope", "owner change's ID"),
        ("dmd_owner_change_" + "0" * 26, "No such owner change"),
    ):
        with pytest.raises(SystemExit) as stopped:
            cli.sign_change(str(keys), did, bad, confirm=lambda _: "yes")
        assert reason in str(stopped.value)
    with pytest.raises(SystemExit, match="Nothing was signed"):
        cli.sign_change(str(keys), did, change_id, confirm=lambda _: "no")
    assert record(gateway, did)["controller_public_key"] == AGENT_CONTROLLER

    cli.sign_change(str(keys), did, change_id, confirm=lambda _: "yes")
    out = capsys.readouterr().out
    assert f"Signed (approve) by {AGENT_CONTROLLER}." in out
    assert f"Still needed: the acceptance of {account}." in out
    assert "Back on the page, click Continue." in out
    # Signing again: nothing left for the key, nothing signed twice.
    cli.sign_change(str(keys), did, change_id, confirm=lambda _: "yes")
    assert "Signed (" not in capsys.readouterr().out

    # The wallet accepts on the page: the change applies.
    accept = item(change, "accept", account)
    done = send(gateway, did, change, accept, wallet_signature(wallet, accept["message"]))
    assert done.json()["status"] == "applied", done.text
    assert record(gateway, issued_did)["controller_wallet"] == account
    with pytest.raises(SystemExit, match="This owner change is applied"):
        cli.sign_change(str(keys), did, change_id, confirm=lambda _: "yes")


@needs_db
def test_the_tool_never_signs_another_gateways_change(
    gateway: TestClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    did = issue(gateway)["did"]
    keys = tmp_path / "did-keys"
    keys.mkdir()
    _key_file(keys, "my-agent.key", AGENT_KEY)
    _key_file(keys, "new.key", Ed25519PrivateKey.generate())
    cli = _tool(gateway)
    change = start(gateway, did, new_controller_wallet_address=Account.create().address)
    cli.API = "https://api.debitmydata.io"  # the messages name this gateway, not that one
    with pytest.raises(SystemExit, match="nothing was signed"):
        cli.sign_change(str(keys), did, change["change_id"], confirm=lambda _: "yes")
    cli.API = BASE
    response = gateway.get(f"/v1/dids/{did}/owner-changes/{change['change_id']}", headers=HOST)
    assert {m["status"] for m in response.json()["messages"]} == {"open"}
