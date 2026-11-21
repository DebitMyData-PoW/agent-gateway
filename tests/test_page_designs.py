"""The chosen design on every page for people (api/page_design.py), not only the Get a DID-LLM
page: each page's inline style is its own followed by the design's, and its Content-Security-
Policy allows exactly that style."""

from __future__ import annotations

import base64
import hashlib
import re
from types import SimpleNamespace

import pytest

from dmd_gateway.api import (
    directory_page,
    docs_pages,
    manage_page,
    oauth_browser,
    page_design,
    policies,
    superhighway_page,
    try_page,
)

from .test_get_did_page import _design_client

STYLE = re.compile(r"<style>(.*?)</style>", re.S)
BROWSER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _check(html: str, csp: str, design: str) -> None:
    """The page carries the design's mark, and its CSP allows its inline style."""
    (style,) = STYLE.findall(html)
    assert f"'sha256-{_sha256(style)}'" in csp
    if design == page_design.CLASSIC:
        assert "data-design=" not in html and "design-" not in style
    else:
        assert f"<html lang=en data-design={design}>" in html
        assert page_design.stylesheet(design) in style
        assert f'href="/fonts/{page_design.FONTS[design][0]}"' in html  # preloaded


PAGE_STYLES = {
    "directory": directory_page.STYLES,
    "manage": manage_page.STYLES,
    "policies": policies.STYLES,
    "superhighway": superhighway_page.STYLES,
    "guided": try_page.STYLES,
    "sign-in": oauth_browser.STYLES,
    "api docs": docs_pages.HELPER_STYLES,
}


@pytest.mark.parametrize("name", PAGE_STYLES)
def test_each_page_has_a_style_per_design(name: str) -> None:
    styles = PAGE_STYLES[name]
    classic = styles[page_design.CLASSIC]
    for design in page_design.DESIGNS:
        assert styles[design].startswith(classic)  # the page's own style comes first
        if design != page_design.CLASSIC:
            assert page_design.stylesheet(design) in styles[design]
    # Aurora's rules for these pages come last, after the design's main stylesheet.
    assert styles["aurora"].endswith(
        (page_design._STATIC / "design-aurora-pages.css").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("design", page_design.DESIGNS)
def test_the_setting_gives_every_page_the_design(design: str) -> None:
    with _design_client(page_design=design, staging_test_tools=True) as client:
        for path in ("/manage", "/policies", "/policies/terms", "/docs", "/docs/advanced"):
            page = client.get(path)
            assert page.status_code == 200, path
            _check(page.text, page.headers["content-security-policy"], design)
            assert "set-cookie" not in page.headers
        page = client.get("/agenticsuperhighway", headers={"accept": BROWSER})
        _check(page.text, page.headers["content-security-policy"], design)


def test_previews_work_on_every_page_and_policies_are_not_kept_then() -> None:
    with _design_client(page_design_preview=True) as client:
        first = client.get("/policies?design=aurora")
        _check(first.text, first.headers["content-security-policy"], "aurora")
        assert first.headers["set-cookie"].startswith(f"{page_design.PREVIEW_COOKIE}=aurora;")
        assert first.headers["cache-control"].startswith("no-cache")
        # The cookie carries the choice to the other pages.
        for path in ("/manage", "/docs/advanced", "/get-did"):
            page = client.get(path)
            assert "data-design=aurora" in page.text, path
        back = client.get("/manage?design=classic")
        _check(back.text, back.headers["content-security-policy"], page_design.CLASSIC)
    with _design_client() as client:  # without previews, the policies may be kept 5 minutes
        page = client.get("/policies?design=aurora")
        assert page.headers["cache-control"].startswith("public, max-age=300")
        assert "data-design=" not in page.text


def _request(design: str) -> SimpleNamespace:
    settings = SimpleNamespace(page_design=design, page_design_preview=False)
    return SimpleNamespace(
        query_params={},
        cookies={},
        url=SimpleNamespace(scheme="https"),
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
    )


@pytest.mark.parametrize("design", page_design.DESIGNS)
def test_the_sign_in_pages_follow_the_design(design: str) -> None:
    page = oauth_browser._problem_page(_request(design), "Something is wrong.")
    assert page.status_code == 400
    _check(page.body.decode(), page.headers["content-security-policy"], design)


@pytest.mark.parametrize("design", page_design.DESIGNS)
def test_the_directory_pages_follow_the_design(design: str) -> None:
    request = _request(design)
    request.app.state.settings.staging_test_tools = False
    request.app.state.settings.app_host = None
    request.headers = {}
    html = directory_page._page(request, "DID-LLM directory", "<p>body</p>", None, design)
    _check(html, directory_page._csp(None, design), design)


def test_unknown_or_missing_designs_fall_back_to_the_classic_look() -> None:
    assert page_design.configured(SimpleNamespace(page_design="neon")) == page_design.CLASSIC
    assert page_design.configured(SimpleNamespace()) == page_design.CLASSIC
    assert page_design.root_attribute(page_design.CLASSIC) == ""
    assert page_design.preload(page_design.CLASSIC) == ""


def test_the_classic_pages_are_unchanged() -> None:
    assert policies.CSP == policies.CSPS[page_design.CLASSIC]
    assert try_page.CSP == try_page.CSPS[page_design.CLASSIC]
    assert docs_pages.SWAGGER_CSP == docs_pages.SWAGGER_CSPS[page_design.CLASSIC]
    assert superhighway_page.CSP == superhighway_page.CSPS[page_design.CLASSIC]
    assert oauth_browser.CSP == oauth_browser.CSPS[page_design.CLASSIC]
