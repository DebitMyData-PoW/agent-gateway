"""The API docs pages: Swagger UI and ReDoc at exact versions checked by fingerprint, nothing
else allowed to run, the how-to guide outside staging, and the values panel."""

from __future__ import annotations

import base64
import hashlib
import json
import re

from fastapi.testclient import TestClient

from dmd_gateway.api import docs_pages
from dmd_gateway.main import HOW_TO_STEPS, STAGING_GUIDED_PAGE

from .test_staging_tools import staging_client  # noqa: F401


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _inline(html: str, tag: str) -> str:
    return re.search(rf"<{tag}>(.*?)</{tag}>", html, re.S).group(1)


def test_swagger_ui_comes_at_an_exact_version_checked_by_fingerprint(client: TestClient) -> None:
    page = client.get("/docs/advanced").text
    for url, integrity in (docs_pages.SWAGGER_UI_JS, docs_pages.SWAGGER_UI_CSS):
        assert f"@{docs_pages.SWAGGER_UI_VERSION}/" in url
        assert re.fullmatch(r"sha384-[A-Za-z0-9+/]{64}", integrity)
        assert f'"{url}" integrity="{integrity}" crossorigin="anonymous"' in page
    assert "swagger-ui-dist@5/" not in page  # no longer "whatever 5.x is newest"
    assert "validator.swagger.io" not in page


def test_only_swagger_ui_and_the_page_own_code_may_run(client: TestClient) -> None:
    response = client.get("/docs/advanced")
    csp = response.headers["content-security-policy"]
    script, style = _inline(response.text, "script"), _inline(response.text, "style")
    assert f"script-src {docs_pages.SWAGGER_UI_JS[0]} 'sha256-{_sha256(script)}';" in csp
    assert f"style-src {docs_pages.SWAGGER_UI_CSS[0]} 'sha256-{_sha256(style)}';" in csp
    for rule in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'"):
        assert rule in csp
    assert "unsafe" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"


def _settings(page: str) -> dict:
    block = r'<script type="application/json" id="swagger-config">(.*?)</script>'
    return json.loads(re.search(block, page, re.S).group(1))


def test_swagger_ui_settings_are_data_not_code(client: TestClient) -> None:
    config = _settings(client.get("/docs/advanced").text)["swagger"]
    assert config["url"] == "/openapi.json"
    assert config["validatorUrl"] is None
    assert config["tryItOutEnabled"] is True and config["deepLinking"] is True


def _check_steps(client: TestClient, path: str, outside: set[int], x402: list[int]) -> list:
    settings = _settings(client.get(path).text)
    steps = settings["steps"]
    assert [step["number"] for step in steps] == list(range(1, 10))
    assert {step["number"] for step in steps if "outside" in step} == outside
    assert settings["x402_order"] == x402
    schema = client.get("/openapi.json").json()
    for step in steps:
        if "outside" in step:
            assert step["outside"] and "pattern" not in step
            continue
        # The endpoint exists, under that section, and the pattern recognises its address.
        matches = [
            (route, method)
            for route, methods in schema["paths"].items()
            for method, operation in methods.items()
            if operation["operationId"] == step["operation_id"]
        ]
        assert len(matches) == 1
        route, method = matches[0]
        assert step["method"] == method.upper()
        assert schema["paths"][route][method]["tags"][0] == step["tag"]
        example = re.sub(r"\{[^}]+\}", "dmd_x_01ABC", route)
        assert re.fullmatch(step["pattern"], example)
        assert not re.fullmatch(step["pattern"], example + "/other")
    return steps


def test_the_production_steps_are_numbered_with_their_endpoints(client: TestClient) -> None:
    steps = _check_steps(client, "/docs/advanced", outside={3, 6, 9}, x402=[1, 2, 5, 6, 7, 8, 9])
    by_number = {step["number"]: step for step in steps}
    assert by_number[4]["done_when"] == {"field": "status", "value": "settled"}
    assert by_number[7]["done_when"] == {"field": "status", "value": "verified"}
    assert {n for n, step in by_number.items() if step.get("restarts")} == {2, 5}
    assert "Never paste a private key" in by_number[6]["outside"]
    # A key and a wallet: after the first verified proof, steps 5 to 7 again for the other.
    assert by_number[7]["again_when"]["value"] == "prove_wallet_control"
    assert "leave PAYMENT-SIGNATURE empty" in by_number[8]["help"]
    assert by_number[3]["needs"] == ["recipient", "amount"]
    assert by_number[6]["needs"] == ["message"]


def test_the_staging_steps_are_numbered_with_their_endpoints(
    staging_client: TestClient,  # noqa: F811
) -> None:
    steps = _check_steps(
        staging_client, "/docs/advanced", outside={9}, x402=[1, 2, 5, 6, 7, 3, 8, 9]
    )
    assert steps[0]["operation_id"].startswith("create_test_agent_key")
    assert steps[2]["operation_id"].startswith("create_test_payment")
    assert "private_key below" in steps[5]["help"] and "PAYMENT-SIGNATURE empty" in steps[7]["help"]
    assert steps[4]["needs"] == ["registration_intent_id", "controller_type"]
    assert steps[5]["needs"] == ["challenge_id", "private_key"]
    assert steps[6]["needs"] == ["registration_intent_id", "challenge_id", "signature"]
    assert steps[6]["again_when"]["field"] == "next_action"


def test_a_challenge_lets_the_gateway_pick_the_controller_by_default(client: TestClient) -> None:
    # Swagger UI would otherwise fill in {"controller_type": "string"}, which is refused.
    schema = client.get("/openapi.json").json()
    assert schema["components"]["schemas"]["ChallengeRequest"]["examples"] == [{}]


def test_the_page_marks_each_step_and_offers_the_next_one() -> None:
    script = docs_pages._HELPER_JS
    assert "OperationSummaryPath" in script and "wrapComponents" in script
    assert "layoutActions.show" in script
    assert '"Go ↓"' in script


def test_redoc_comes_at_an_exact_version_checked_by_fingerprint(client: TestClient) -> None:
    response = client.get("/redoc")
    url, integrity = docs_pages.REDOC_JS
    assert f"redoc@{docs_pages.REDOC_VERSION}/" in url
    assert f'"{url}" integrity="{integrity}" crossorigin="anonymous"' in response.text
    csp = response.headers["content-security-policy"]
    assert f"script-src {url};" in csp and "default-src 'none'" in csp
    assert "fonts.googleapis.com" not in response.text


def test_the_values_panel_keeps_values_in_the_page_only() -> None:
    script = docs_pages._HELPER_JS
    for field in (
        "registration_intent_id",
        "payment_intent_id",
        "challenge_id",
        "message",
        "payment_signature",
        "did_document_url",
    ):
        assert f'"{field}"' in script
    for storage in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "sendBeacon",
        "XMLHttpRequest",
        "fetch(",
    ):
        assert storage not in script
    # Private keys from the staging test tools are only taken from those tools' answers.
    assert script.count("staging: true") == 2


def test_the_docs_page_explains_how_to_register_by_hand(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    guide = schema["info"]["description"]
    assert "### Register a DID from this page" in guide
    assert f"]({STAGING_GUIDED_PAGE})" in guide
    assert "Never paste a private key" in guide
    assert "[Get a DID](/get-did)" in guide
    assert "Values from your responses" in guide
    assert "15 minutes of step 2" in guide and "5 minutes of step 5" in guide
    operations = {
        (operation["tags"][0], operation["operationId"])
        for methods in schema["paths"].values()
        for operation in methods.values()
    }
    links = re.findall(r"\]\(#/([a-z]+)/([A-Za-z0-9_]+)\)", guide)
    assert len(links) == sum(1 for request, _ in HOW_TO_STEPS if request)
    assert set(links) <= operations  # every link opens a real endpoint


def test_staging_keeps_its_own_guide_and_the_panel_on_the_advanced_page(
    staging_client: TestClient,  # noqa: F811
) -> None:
    guide = staging_client.get("/openapi.json").json()["info"]["description"]
    assert "### Register a DID from this page" not in guide
    assert "Try the whole registration from this page" in guide
    advanced = staging_client.get("/docs/advanced")
    assert advanced.headers["content-security-policy"] == docs_pages.SWAGGER_CSP
    assert _inline(advanced.text, "script") == docs_pages._HELPER_JS


def test_the_advanced_page_opens_like_the_guided_pages(
    client: TestClient,
    staging_client: TestClient,  # noqa: F811
) -> None:
    production = client.get("/docs/advanced").text
    assert "<h1>Advanced: API docs</h1>" in production
    assert "Every step here is real:" in production
    assert 'href="/get-did">Easiest: Get a DID' in production
    assert f'href="{STAGING_GUIDED_PAGE}"' in production
    for section in ("id=adv-steps", "id=adv-guide", "All endpoints"):
        assert section in production
    staging = staging_client.get("/docs/advanced").text
    assert "Staging server." in staging and 'href="/docs">Easiest: the guided test' in staging
    assert 'href="/get-did">Get a DID with your wallet' in staging


def test_each_step_card_has_its_text_and_endpoint(
    client: TestClient,
    staging_client: TestClient,  # noqa: F811
) -> None:
    for server in (client, staging_client):
        data = server.app.state.docs_steps()
        for step in data["steps"]:
            about = step["about"]
            assert about[:1].isupper() and not about.startswith("**"), step
            assert "{" not in about.replace("{payment_intent_id}", ""), step  # filled in
            assert ("label" in step) is ("operation_id" in step), step
        assert set(data["order_notes"]) == {"dmd", "x402"}
        assert data["x402_open"] in (True, False)
    # The test settings open no payment option: the page shows USDC as not open yet.
    assert client.app.state.docs_steps()["x402_open"] is False
