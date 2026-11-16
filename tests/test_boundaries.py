"""The gateway's authentication boundaries (roadmap GW-09; docs/architecture.md, "Authentication
boundaries"): every address the production gateway answers, with the rule that guards it. A new
address fails this test until it is added here with its rule, so none appears unnoticed; and
none is for operators or tests: they act on the server only (dmd-gateway-admin, gateway.env).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from dmd_gateway.main import create_app
from dmd_gateway.oauth import keys
from dmd_gateway.settings import Settings

from .conftest import UNREACHABLE_DB
from .test_staging_tools import staging_settings

# What guards an address.
PUBLIC = "public: reads what anyone may know (pages, documents, the public record)"
BY_ID = (
    "reads one record by its random ID, given to whoever made it (an issued DID-LLM's "
    "credential, its public receipt, names its registration and payment)"
)
QUOTE = "makes only an expiring quote or message to sign: nothing is owed or changed"
SIGNED = "changes something only with the owner's own signature, checked by the gateway"
PAID = "acts only on a settled payment (or the payer's own signed x402 payment)"
CHECK = "changes nothing: a check, or a hint the gateway checks on the chain itself"
OAUTH = "the sign-in server: at its own host only, signed requests and tokens (RFC 6749)"
AGENT = "MCP and A2A: each tool or skill keeps its own rule; tools that need a sign-in, a token"

INVENTORY = {
    ("GET", "/"): PUBLIC,
    ("GET", "/.well-known/agent-card.json"): PUBLIC,
    ("OPTIONS", "/.well-known/agent-card.json"): PUBLIC,
    ("GET", "/.well-known/agent-skills/debitmydata-did-llm/SKILL.md"): PUBLIC,
    ("GET", "/.well-known/agent-skills/index.json"): PUBLIC,
    ("GET", "/.well-known/ai-catalog.json"): PUBLIC,
    ("OPTIONS", "/.well-known/ai-catalog.json"): PUBLIC,
    ("GET", "/.well-known/api-catalog"): PUBLIC,
    ("GET", "/.well-known/did.json"): PUBLIC,
    ("GET", "/.well-known/google-connected-apps"): PUBLIC,
    ("GET", "/.well-known/jwks.json"): PUBLIC,
    ("GET", "/.well-known/mcp"): PUBLIC,
    ("OPTIONS", "/.well-known/mcp"): PUBLIC,
    ("GET", "/.well-known/mcp/server-card.json"): PUBLIC,
    ("OPTIONS", "/.well-known/mcp/server-card.json"): PUBLIC,
    ("GET", "/.well-known/oauth-authorization-server"): PUBLIC,
    ("GET", "/.well-known/oauth-protected-resource"): PUBLIC,
    ("GET", "/.well-known/oauth-protected-resource/agenticsuperhighway"): PUBLIC,
    ("GET", "/.well-known/oauth-protected-resource/agenticsuperhighway/identity"): PUBLIC,
    ("GET", "/.well-known/oauth-protected-resource/mcp"): PUBLIC,
    ("GET", "/.well-known/security.txt"): PUBLIC,
    ("*", "/agenticsuperhighway"): AGENT,
    ("POST", "/agenticsuperhighway/a2a"): AGENT,
    ("GET", "/agenticsuperhighway/server-card"): PUBLIC,
    ("OPTIONS", "/agenticsuperhighway/server-card"): PUBLIC,
    ("GET", "/agents/{suffix}/did.json"): PUBLIC,
    ("GET", "/app/{name}"): PUBLIC,
    ("GET", "/authorize"): OAUTH,
    ("GET", "/authorize/{sign_in_id}"): OAUTH,
    ("POST", "/authorize/{sign_in_id}/deny"): OAUTH,
    ("POST", "/authorize/{sign_in_id}/did"): OAUTH,
    ("POST", "/authorize/{sign_in_id}/finish"): OAUTH,
    ("GET", "/authorize/{sign_in_id}/request"): OAUTH,
    ("POST", "/authorize/{sign_in_id}/signature"): OAUTH,
    ("POST", "/challenge"): OAUTH,
    ("GET", "/directory"): PUBLIC,
    ("GET", "/directory/list"): PUBLIC,
    ("GET", "/directory/{suffix}"): PUBLIC,
    ("GET", "/docs"): PUBLIC,
    ("GET", "/docs/advanced"): PUBLIC,
    ("GET", "/favicon.ico"): PUBLIC,
    ("GET", "/favicon.png"): PUBLIC,
    ("GET", "/fonts/{name}"): PUBLIC,
    ("GET", "/get-did"): PUBLIC,
    ("GET", "/healthz"): PUBLIC,
    ("GET", "/llms.txt"): PUBLIC,
    ("GET", "/logo-dark.png"): PUBLIC,
    ("GET", "/logo.png"): PUBLIC,
    ("GET", "/manage"): PUBLIC,
    ("GET", "/manifest.webmanifest"): PUBLIC,
    ("*", "/mcp"): AGENT,
    ("GET", "/mcp/server-card"): PUBLIC,
    ("OPTIONS", "/mcp/server-card"): PUBLIC,
    ("GET", "/openapi.json"): PUBLIC,
    ("GET", "/policies"): PUBLIC,
    ("GET", "/policies/{name}"): PUBLIC,
    ("GET", "/readyz"): PUBLIC,
    ("GET", "/redoc"): PUBLIC,
    ("POST", "/register"): OAUTH,
    ("POST", "/revoke"): OAUTH,
    ("GET", "/robots.txt"): PUBLIC,
    ("GET", "/sitemap.xml"): PUBLIC,
    ("GET", "/sw.js"): PUBLIC,
    ("POST", "/token"): OAUTH,
    ("GET", "/v1/credentials/{credential_id}"): PUBLIC,
    ("POST", "/v1/dids/registration-quotes"): QUOTE,
    ("POST", "/v1/dids/registrations"): PAID,
    ("GET", "/v1/dids/registrations/{registration_intent_id}"): BY_ID,
    ("POST", "/v1/dids/registrations/{registration_intent_id}/controller-challenges"): QUOTE,
    ("POST", "/v1/dids/registrations/{registration_intent_id}/controller-proofs"): SIGNED,
    ("POST", "/v1/dids/verifications"): CHECK,
    ("GET", "/v1/dids/{identity_id}"): PUBLIC,
    ("POST", "/v1/dids/{identity}/owner-changes"): QUOTE,
    ("GET", "/v1/dids/{identity}/owner-changes/{change_id}"): BY_ID,
    ("POST", "/v1/dids/{identity}/owner-changes/{change_id}/signatures"): SIGNED,
    ("POST", "/v1/dids/{identity}/revocations"): QUOTE,
    ("GET", "/v1/dids/{identity}/revocations/{challenge_id}"): BY_ID,
    ("POST", "/v1/dids/{identity}/revocations/{challenge_id}/signature"): SIGNED,
    ("GET", "/v1/directory"): PUBLIC,
    ("GET", "/v1/directory/challenges/{challenge_id}"): BY_ID,
    ("GET", "/v1/directory/{identity}"): PUBLIC,
    ("POST", "/v1/directory/{identity}/challenges"): QUOTE,
    ("POST", "/v1/directory/{identity}/proofs"): SIGNED,
    ("GET", "/v1/discovery"): PUBLIC,
    ("POST", "/v1/donations/{name}"): PAID,
    ("GET", "/v1/donations/{purchase_id}"): BY_ID,
    ("GET", "/v1/entitlements/{identity_id}"): PUBLIC,
    ("GET", "/v1/payment-options"): PUBLIC,
    ("GET", "/v1/payments/{payment_intent_id}"): BY_ID,
    ("POST", "/v1/payments/{payment_intent_id}/references"): CHECK,
    ("GET", "/v1/policies"): PUBLIC,
    ("GET", "/v1/prices/dmd-usd"): PUBLIC,
    ("GET", "/v1/products"): PUBLIC,
    ("GET", "/v1/products/purchases/{purchase_id}"): BY_ID,
    ("POST", "/v1/products/purchases/{purchase_id}/complete"): PAID,
    ("POST", "/v1/products/{product}/quotes"): QUOTE,
}
SIGN_IN_ONLY = {("*", "/agenticsuperhighway/identity"): OAUTH}  # every tool there needs a token
FORBIDDEN = re.compile(r"admin|internal|operator|debug|staging|metrics")


def routes(app: FastAPI) -> set[tuple[str, str]]:
    """Every (method, path) the app answers (HEAD aside): its routes, those of the routers it
    includes, and the MCP endpoints mounted as plain routes ("*": the MCP transport's own)."""
    found: set[tuple[str, str]] = set()

    def walk(items: Iterable[Any], prefix: str = "") -> None:
        for route in items:
            included = getattr(route, "original_router", None)
            if included is not None:  # FastAPI's included router
                context = getattr(route, "include_context", None)
                walk(included.routes, prefix + (getattr(context, "prefix", "") or ""))
            elif hasattr(route, "endpoint") and hasattr(route, "path"):
                methods = getattr(route, "methods", None) or {"*"}
                for method in set(methods) - {"HEAD"}:
                    found.add((method, prefix + route.path))
            elif hasattr(route, "routes"):  # a mounted router
                walk(route.routes, prefix + getattr(route, "path", ""))
            else:
                found.add(("*", prefix + str(getattr(route, "path", repr(route)))))

    walk(app.routes)
    assert len(found) > 50, "the routes were not found: has FastAPI changed how it keeps them?"
    return found


def app_for(settings: Settings) -> FastAPI:
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    return create_app(settings, environ={}, engine=engine)


def production(settings: Settings, **changes: Any) -> Settings:
    values = {
        "environment": "production",
        "public_base_url": "https://api.debitmydata.io",
        "app_host": "id.debitmydata.io",
    }
    return settings.model_copy(update={**values, **changes})


def test_production_answers_exactly_the_addresses_listed_here(settings: Settings) -> None:
    found = routes(app_for(production(settings)))
    assert sorted(found - set(INVENTORY)) == [], "new addresses: add each with its rule"
    assert sorted(set(INVENTORY) - found) == [], "gone: remove them from the list"


def test_no_address_for_operators_or_tests_and_every_write_has_a_rule() -> None:
    assert not [path for _, path in INVENTORY if FORBIDDEN.search(path)]
    for (method, path), rule in INVENTORY.items():
        if method not in ("GET", "OPTIONS"):
            assert rule in (QUOTE, SIGNED, PAID, CHECK, OAUTH, AGENT), path
        if rule == BY_ID:
            assert method == "GET" and re.search(r"\{[a-z_]+_id\}$", path), path


def test_sign_in_adds_only_the_identity_endpoint(settings: Settings, tmp_path: Path) -> None:
    key = tmp_path / "oauth.pem"
    keys.write_new_key(key)
    auth = "auth.localhost.test"
    app = app_for(settings.model_copy(update={"auth_host": auth, "oauth_key_path": key}))
    assert routes(app) == set(INVENTORY) | set(SIGN_IN_ONLY)
    with TestClient(app) as client:
        # The sign-in server answers at its host only, and only the sign-in server answers there.
        assert client.post("/token", data={"grant_type": "client_credentials"}).status_code == 404
        assert client.get("/v1/discovery", headers={"host": auth}).status_code == 404
        assert client.get("/healthz").status_code == 200


def test_the_staging_test_tools_add_staging_addresses_only() -> None:
    """The test tools (test keys, payments, signatures; never in production, Settings refuses
    them there) and the page that uses them."""
    found = routes(app_for(staging_settings()))
    extra = sorted(path for _, path in found - set(INVENTORY))
    assert extra[0] == "/try"  # the guided test page
    assert extra[1:] and all(path.startswith("/v1/staging/test-") for path in extra[1:]), extra
