from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_unavailable_database_without_details(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body == {
        "status": "unavailable",
        "checks": {"payment_route_registry": "ok", "database": "unavailable"},
    }
    assert "nobody" not in response.text  # no connection details leak


def test_discovery_describes_only_live_capabilities(client: TestClient) -> None:
    body = client.get("/v1/discovery").json()
    assert body["service"] == "DebitMyData Agent Gateway"
    assert body["mcp_url"] is None  # not advertised until configured
    (offer,) = body["offers"]
    assert offer["id"] == "did_registration"
    assert offer["commercial_price"] == {"currency": "USD", "amount": "25.00"}
    assert offer["quote_required"] is True
    assert offer["accepting_payments"] is False  # no rail is enabled yet
    assert offer["method"] == "POST"
    assert offer["resource"].endswith("/v1/dids/registrations")
    assert "commercial fee of $25.00 USD" in offer["description"]
    quote, registration = offer["endpoints"]["quote"], offer["endpoints"]["registration"]
    assert quote["url"].endswith("/v1/dids/registration-quotes")
    assert quote["available"] is False  # no payment rail is enabled
    assert registration["available"] is False  # no rail enabled and no issuer key
    assert registration["url"].endswith("/v1/dids/registrations")
    challenge = offer["endpoints"]["controller_challenge"]["url"]
    assert challenge.endswith("/registrations/{registration_intent_id}/controller-challenges")
    assert body["credential_issuer"] is None  # no issuer key configured
    assert "terms" in body["policy_urls"]


def test_payment_options_lists_only_enabled_rails(client: TestClient) -> None:
    response = client.get("/v1/payment-options", params={"service": "did_registration"})
    assert response.status_code == 200
    body = response.json()
    assert body["commercial_fee"] == {"currency": "USD", "amount": "25.00"}
    assert body["service"] == "DebitMyData Agentic DID Registration"
    assert body["supported_payment_options"] == []
    assert body["notice"] == (
        "Payment availability may vary by asset, network, wallet policy, geographic "
        "eligibility, and DebitMyData risk controls."
    )


def test_unknown_service_is_404(client: TestClient) -> None:
    assert client.get("/v1/payment-options", params={"service": "nope"}).status_code == 404


def test_correlation_id_is_generated(client: TestClient) -> None:
    response = client.get("/healthz")
    assert len(response.headers["x-request-id"]) == 32


def test_well_formed_correlation_id_is_echoed(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "agent-req-12345"})
    assert response.headers["x-request-id"] == "agent-req-12345"


def test_malformed_correlation_id_is_replaced(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "bad id\nwith newline"})
    assert response.headers["x-request-id"] != "bad id\nwith newline"
    assert len(response.headers["x-request-id"]) == 32


def test_openapi_is_published(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert {
        "/healthz",
        "/readyz",
        "/v1/discovery",
        "/v1/payment-options",
        "/v1/dids/registration-quotes",
        "/v1/payments/{payment_intent_id}",
        "/v1/dids/registrations",
        "/v1/dids/registrations/{registration_intent_id}",
        "/v1/dids/registrations/{registration_intent_id}/controller-challenges",
        "/v1/dids/registrations/{registration_intent_id}/controller-proofs",
        "/v1/dids/{identity_id}",
        "/v1/credentials/{credential_id}",
        "/v1/entitlements/{identity_id}",
        "/agents/{suffix}/did.json",
        "/.well-known/did.json",
    } <= set(paths)
