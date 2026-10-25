"""The policy pages served by the gateway (DEC-09): every page exists and is filled in for
the server it runs on, quotes and discovery link to them, and the terms version that quotes
name is the version of the terms page."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from dmd_gateway.domain.routes import RegistryError, load_registry
from dmd_gateway.main import create_app
from dmd_gateway.services import policy_pages
from dmd_gateway.services.policy_pages import PAGES, load_policy_pages, policy_links
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, REPO_ROOT, UNREACHABLE_DB

REGTEST_PATH = REPO_ROOT / "config" / "payment_routes.regtest.yaml"
CONFIGS = [REGISTRY_PATH, REGTEST_PATH]
BASE = "https://api.debitmydata.io"


def _pages(path: Path = REGISTRY_PATH, base: str = BASE) -> dict[str, policy_pages.PolicyPage]:
    registry = load_registry(path, environ={}, available_adapters=set())
    return load_policy_pages(
        public_base_url=base, registry=registry, quote_minutes=15, challenge_minutes=5
    )


def _app(environment: str, base: str) -> TestClient:
    settings = Settings(
        _env_file=None,
        environment=environment,
        database_url=UNREACHABLE_DB,
        database_connect_timeout_seconds=1,
        route_registry_path=REGISTRY_PATH,
        public_base_url=base,
    )
    return TestClient(create_app(settings, environ={}, engine=create_engine(UNREACHABLE_DB)))


def test_every_page_is_complete() -> None:
    pages = _pages()
    assert list(pages) == list(PAGES)
    for page in pages.values():
        assert page.status in ("draft", "approved")
        assert page.markdown.startswith(f"# {page.title}\n")
        assert "{{" not in page.markdown and "}}" not in page.markdown, page.name
        assert page.html.startswith(f"<h1>{page.title}</h1>")
        # Links to other pages point to pages that exist.
        for linked in re.findall(rf"{BASE}/policies/([a-z-]+)", page.markdown):
            assert linked in PAGES, (page.name, linked)


def test_pages_are_filled_in_for_their_server() -> None:
    production = _pages()
    assert "did:web:api.debitmydata.io:agents:<id>" in production["did"].markdown
    assert "`did:web:api.debitmydata.io`" in production["security"].markdown
    assert "15 minutes after it was made" in production["payment-methods"].markdown
    assert "valid for\n5 minutes" in production["did"].markdown

    staging = _pages(REGTEST_PATH, "https://staging-api.debitmydata.io")
    assert "did:web:staging-api.debitmydata.io:agents:<id>" in staging["did"].markdown


def test_payment_methods_list_only_approved_rails() -> None:
    table = _pages()["payment-methods"].markdown
    assert "| DMD | DebitMyData PoW (`dmd:mainnet`) | native coin | 6 |" in table
    usdc = "| USDC | Base (`eip155:8453`) | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | 5 |"
    assert usdc in table
    assert "x402_dmd_wrapped_base" not in table and "solana_usdc" not in table

    staging = _pages(REGTEST_PATH)["payment-methods"].markdown
    assert "`dmd:regtest`" in staging and "`eip155:84532`" in staging
    assert "`eip155:8453`)" not in staging


@pytest.mark.parametrize("path", CONFIGS, ids=["production", "staging"])
def test_quotes_name_the_version_of_the_terms_page(path: Path) -> None:
    registry = load_registry(path, environ={}, available_adapters=set())
    terms = _pages(path)["terms"]
    for service in registry.services.values():
        assert service.terms_version == terms.version.isoformat()


@pytest.mark.parametrize("path", CONFIGS, ids=["production", "staging"])
def test_policy_links_point_to_served_pages(path: Path) -> None:
    services = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]
    for service in services.values():
        urls = service["policy_urls"]
        # The directive's list: terms, refund, privacy and DID policies, plus payments.
        for key in ("terms", "privacy", "security", "payment_policy", "refund_policy"):
            assert key in urls
        assert urls["did_policy"] == "/policies/did"
        for key, url in urls.items():
            if url.startswith("/"):
                name = url.removeprefix("/policies/")
                assert PAGES[name] == key, (key, url)


def test_links_become_full_urls_on_this_server() -> None:
    links = policy_links({"terms": "/policies/terms", "site": "https://example.com/"}, BASE + "/")
    assert links == {"terms": f"{BASE}/policies/terms", "site": "https://example.com/"}


@pytest.mark.parametrize("url", ["http://example.com/terms", "//example.com/terms", "terms"])
def test_policy_urls_must_be_paths_or_https(tmp_path: Path, url: str) -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    data["services"]["did_registration"]["policy_urls"]["terms"] = url
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RegistryError, match="policy_urls.terms"):
        load_registry(path, environ={}, available_adapters=set())


def test_pages_are_served_as_html_and_markdown(client: TestClient) -> None:
    index = client.get("/policies")
    assert index.status_code == 200
    for name in PAGES:
        assert f'href="/policies/{name}"' in index.text

    page = client.get("/policies/refunds")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<h1>Refund policy</h1>" in page.text
    assert "<script" not in page.text
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    # The style the page carries is the one its policy allows.
    style = re.search(r"<style>(.*?)</style>", page.text, re.DOTALL)
    assert style is not None
    digest = base64.b64encode(hashlib.sha256(style.group(1).encode()).digest()).decode()
    assert f"'sha256-{digest}'" in csp

    markdown = client.get("/policies/refunds.md")
    assert markdown.headers["content-type"] == "text/markdown; charset=utf-8"
    assert markdown.text.startswith("# Refund policy\n")

    listing = client.get("/v1/policies").json()
    assert listing["terms_version"] == "2026-10-25"
    refunds = next(p for p in listing["policies"] if p["name"] == "refunds")
    assert refunds["url"] == "http://localhost:8000/policies/refunds"
    assert refunds["sha256"] == hashlib.sha256(markdown.content).hexdigest()

    missing = client.get("/policies/nothing")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")


def test_raw_html_in_a_text_is_shown_not_run() -> None:
    rendered = policy_pages._MARKDOWN.render("Hello <script>alert(1)</script>")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_test_server_notice_only_outside_production() -> None:
    with _app("staging", "https://staging-api.debitmydata.io") as staging:
        assert "Test server." in staging.get("/policies/terms").text
    with _app("production", BASE) as production:
        page = production.get("/policies/terms").text
        assert "Test server." not in page
        assert "Draft." in page  # until DebitMyData approves the texts
