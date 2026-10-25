"""Application factory.

Run locally with:
    uvicorn --factory dmd_gateway.main:create_app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine

from dmd_gateway import __version__
from dmd_gateway.adapters import PaymentAdapter
from dmd_gateway.api import (
    discovery,
    docs_pages,
    health,
    identities,
    payments,
    policies,
    prices,
    quotes,
    registrations,
    staging,
)
from dmd_gateway.api.form_bodies import FormBodyMiddleware, FormHints, add_form_bodies
from dmd_gateway.db.session import build_engine, session_factory
from dmd_gateway.domain.routes import RailState
from dmd_gateway.errors import install_error_handlers
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.identity.issuer import IssuerKey, load_issuer_key
from dmd_gateway.identity.wallets import WalletVerifier, controller_networks
from dmd_gateway.mcp_server import mount_mcp
from dmd_gateway.observability import CorrelationIdMiddleware, configure_logging
from dmd_gateway.runtime import PriceSourceFactory, build_runtime
from dmd_gateway.services.policy_pages import load_policy_pages
from dmd_gateway.services.quotes import DID_REGISTRATION, QuoteContext
from dmd_gateway.services.registrations import IssuanceContext
from dmd_gateway.settings import Settings, get_settings, runtime_environ

log = logging.getLogger("dmd_gateway")


def create_app(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    engine: Engine | None = None,
    adapters: Mapping[str, type[PaymentAdapter]] | None = None,
    price_sources: Mapping[str, PriceSourceFactory] | None = None,
    issuer_key: IssuerKey | None = None,
) -> FastAPI:
    """Build the application.

    ``adapters`` and ``price_sources`` default to the implemented ones, and the issuer key is
    read from ``issuer_key_path`` when set; tests pass their own.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    env = runtime_environ() if environ is None else environ
    runtime = build_runtime(settings, env, adapters=adapters, price_sources=price_sources)
    registry = runtime.registry
    engine = engine or build_engine(settings)
    if issuer_key is None and settings.issuer_key_path is not None:
        issuer_key = load_issuer_key(settings.issuer_key_path, issuer_did(settings.public_base_url))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rails = {rail_id: registry.rail_state(rail_id) for rail_id in registry.rails}
        log.info(
            "agent gateway starting",
            extra={
                "event": {
                    "environment": settings.environment,
                    "enabled_rails": [r for r, s in rails.items() if s is RailState.ENABLED],
                    "killed_rails": [r for r, s in rails.items() if s is RailState.KILLED],
                    "did_issuance": issuer_key is not None,
                }
            },
        )
        async with app.state.mcp.session_manager.run():
            yield
        engine.dispose()

    app = FastAPI(
        title="DebitMyData Agent Gateway",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=_openapi_tags(settings),
        docs_url=None,  # served by docs_pages, with the DebitMyData favicon
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.environ = env
    app.state.staging_payer = None  # created on first use (staging test tools only)
    app.state.registry = registry
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    for source in runtime.price_sources.values():
        source.bind(app.state.session_factory)
    app.state.quote_context = QuoteContext(
        registry=registry,
        adapters=runtime.adapters,
        price_sources=runtime.price_sources,
        quote_ttl=timedelta(seconds=settings.quote_ttl_seconds),
        max_open_quotes=settings.max_open_quotes_per_controller,
        public_base_url=settings.public_base_url,
    )
    app.state.policy_pages = load_policy_pages(
        public_base_url=settings.public_base_url,
        registry=registry,
        quote_minutes=settings.quote_ttl_seconds // 60,
        challenge_minutes=settings.controller_challenge_ttl_seconds // 60,
    )
    app.state.wallet_verifier = WalletVerifier.from_registry(registry, env)
    app.state.issuance = IssuanceContext(
        registry=registry, issuer=issuer_key, public_base_url=settings.public_base_url
    )

    install_error_handlers(app)
    if settings.staging_test_tools:
        app.add_middleware(FormBodyMiddleware)  # docs page forms; see api/form_bodies.py
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(discovery.router)
    app.include_router(prices.router)
    app.include_router(quotes.router)
    app.include_router(payments.router)
    app.include_router(registrations.router)
    app.include_router(identities.router)
    app.include_router(policies.router)
    app.include_router(docs_pages.router)
    if settings.staging_test_tools:
        app.include_router(staging.router)
        _add_try_it_guide(app)
    app.state.mcp = mount_mcp(app, settings)
    return app


DESCRIPTION = (
    "Public integration surface for agents: discovery, payment options, quotes, payment "
    "verification, controller proofs and DebitMyData DID issuance with signed credentials. "
    "Payment options appear only once their payment rail is enabled. AI agents can use the "
    "same steps through the MCP server at /mcp. Terms, privacy, payment, refund, DID and "
    "security policies: [/policies](/policies)."
)


def _openapi_tags(settings: Settings) -> list[dict[str, str]]:
    tags = [
        {"name": "discovery", "description": "What DebitMyData offers and how agents can pay."},
        {
            "name": "staging",
            "description": "Staging only: test tools that stand in for an agent's own key and "
            "wallet, on test networks whose coins have no value (a private DMD chain, and Base "
            "Sepolia for USDC). Not available in production.",
        },
        {
            "name": "registration",
            "description": "DID registration: quote, proof of key control, issuance, progress.",
        },
        {"name": "payments", "description": "Payment status and transaction references."},
        {
            "name": "identities",
            "description": "Issued DIDs, signed credentials, entitlements and DID documents.",
        },
        {"name": "health", "description": "Liveness and readiness checks."},
    ]
    if not settings.staging_test_tools:
        tags = [tag for tag in tags if tag["name"] != "staging"]
    return tags


TRY_IT_STEPS = [
    (
        "post",
        "/v1/staging/test-agent-keys",
        "/v1/staging/test-agent-keys",
        "a test agent key and wallet. Keep `controller_public_key` and `private_key` (and "
        "`controller_wallet_address` and `wallet_private_key` to try a wallet, see below).",
    ),
    (
        "post",
        "/v1/dids/registration-quotes",
        "/v1/dids/registration-quotes",
        "paste your `controller_public_key`, and pick how to pay in "
        "`preferred_payment_option`: `dmd_pow_native` (DMD) or `x402_usdc_base` (USDC). "
        "Keep `registration_intent_id` (starts with `dmd_did_intent_`) and "
        "`payment_intent_id` (starts with `dmd_payment_intent_`).",
    ),
    (
        "post",
        "/v1/staging/test-payments",
        "/v1/staging/test-payments",
        "your `payment_intent_id`. DMD: test coins pay the quote and 6 blocks confirm "
        "it. USDC: see below.",
    ),
    (
        "get",
        "/v1/payments/{payment_intent_id}",
        "/v1/payments/{payment_intent_id}",
        "your `payment_intent_id`. After about 10 seconds, `status` is `settled`.",
    ),
    (
        "post",
        "/v1/dids/registrations/{registration_intent_id}/controller-challenges",
        "/v1/dids/registrations/{id}/controller-challenges",
        "your `registration_intent_id` (leave `controller_type` empty). Keep `challenge_id`.",
    ),
    (
        "post",
        "/v1/staging/test-signatures",
        "/v1/staging/test-signatures",
        "your `challenge_id` and `private_key`. Keep `signature` (the short one, for step 7).",
    ),
    (
        "post",
        "/v1/dids/registrations/{registration_intent_id}/controller-proofs",
        "/v1/dids/registrations/{id}/controller-proofs",
        "your `registration_intent_id`, `challenge_id` and the `signature` from step 6. "
        "The status is `verified`.",
    ),
    (
        "post",
        "/v1/dids/registrations",
        "/v1/dids/registrations",
        "your `registration_intent_id`. You get the DID, its signed credential and the "
        "`dmd_did_holder` entitlement.",
    ),
]


# The staging docs page forms: the payment options to pick from are added when the page is
# built.
FORM_HINTS = FormHints(
    defaults={"display_name": "TestAgent-01", "participant_type": "autonomous_agent"},
)


def _add_try_it_guide(app: FastAPI) -> None:
    """Staging docs page: request bodies as input fields, and the steps to run the whole
    registration from the page, each step linked to its endpoint (#/<section>/<operation
    id>, opened by Swagger UI's deep linking)."""
    build = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = build()
        registry = app.state.registry
        options = [rail_id for rail_id, _ in registry.enabled_rails(DID_REGISTRATION)]
        networks, default_network = controller_networks(registry)
        choices = {
            "preferred_payment_option": options,
            "controller_network": networks,
            "controller_type": ["ed25519", "eip155"],
        }
        add_form_bodies(schema, replace(FORM_HINTS, choices=choices))
        lines = [
            "### Try the whole registration from this page",
            "",
            "This is the **staging** server: test networks whose coins have no value. Open "
            "each endpoint below, fill in the fields, click **Execute**, and copy the values "
            "named into the next step. The three **staging** endpoints stand in for what a "
            "real agent does with its own key and wallet.",
            "",
        ]
        for number, (method, path, label, text) in enumerate(TRY_IT_STEPS, start=1):
            operation = schema["paths"][path][method]
            tag = operation["tags"][0]
            link = f"#/{tag}/{operation['operationId']}"
            lines.append(f"{number}. **{tag}** › [{method.upper()} {label}]({link}): {text}")
        lines += [
            f"{len(TRY_IT_STEPS) + 1}. Open the `did_document_url` from step "
            f"{len(TRY_IT_STEPS)} in your browser.",
            "",
            "**Paying with DMD** (`dmd_pow_native`): steps **1 → 2 → 3 → 4 → 5 → 6 → 7 → 8**. "
            "Do step 3 within 15 minutes of step 2, and steps 5 to 7 within 5 minutes.",
            "",
            "**Paying with USDC through x402** (`x402_usdc_base`, Base Sepolia test USDC): "
            "steps **1 → 2 → 5 → 6 → 7 → 3 → 8**, without step 4, all within 15 minutes of "
            "step 2. Step 3 then returns a long `payment_signature` (it starts with `eyJ`). "
            "Paste it into the `PAYMENT-SIGNATURE` field of step 8 and click **Execute** "
            "right away: it is valid for 5 minutes. If it has expired, run step 3 again for a "
            "new one. The payment is settled on Base Sepolia, checked on chain, and the DID "
            "is issued in the same answer.",
            "",
            "**Which value goes where:**",
            "",
            "- `registration_intent_id` (starts with `dmd_did_intent_`): steps 5, 7 and 8.",
            "- `payment_intent_id` (starts with `dmd_payment_intent_`): steps 3 and 4.",
            "- `signature` from step 6 (short): step 7.",
            "- `payment_signature` from step 3 (long, USDC only): step 8, in the "
            "`PAYMENT-SIGNATURE` field.",
            "",
            "**A wallet as the controller** (with the key, or instead of it): in step 2, paste "
            "the `controller_wallet_address` from step 1 as well, or alone with "
            f"`controller_public_key` left empty (its network is `{default_network}` unless "
            "you pick another in `controller_network`). Then do steps 5 to 7 once for each "
            "controller. Step 5 gives the next one still to prove (or pick it in "
            "`controller_type`). In step 6, use `private_key` when the challenge's "
            "`controller` starts with `ed25519:`, and `wallet_private_key` when it starts "
            "with `eip155:`; the wallet's `signature` starts with `0x`. Step 8 then issues a "
            "DID that lists the wallet. `GET /v1/dids/registrations/{id}` shows in "
            "`still_to_prove` what is left.",
            "",
            "Request bodies are shown here as fields to fill in. Agents send the same fields "
            "as JSON: pick `application/json` above a request body to see it.",
        ]
        schema["info"]["description"] += "\n\n" + "\n".join(lines)
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
