"""Application factory.

Run locally with:
    uvicorn --factory dmd_gateway.main:create_app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy import Engine

from dmd_gateway import __version__
from dmd_gateway.adapters import PaymentAdapter
from dmd_gateway.api import discovery, health, identities, payments, quotes, registrations
from dmd_gateway.db.session import build_engine, session_factory
from dmd_gateway.domain.routes import RailState
from dmd_gateway.errors import install_error_handlers
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.identity.issuer import IssuerKey, load_issuer_key
from dmd_gateway.observability import CorrelationIdMiddleware, configure_logging
from dmd_gateway.runtime import PriceSourceFactory, build_runtime
from dmd_gateway.services.quotes import QuoteContext
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
        yield
        engine.dispose()

    app = FastAPI(
        title="DebitMyData Agent Gateway",
        version=__version__,
        description=(
            "Public integration surface for agents: discovery, payment options, quotes, "
            "payment verification, controller proofs and DebitMyData DID issuance with signed "
            "credentials. In development: payment options appear only once their payment rail "
            "is enabled."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    app.state.quote_context = QuoteContext(
        registry=registry,
        adapters=runtime.adapters,
        price_sources=runtime.price_sources,
        quote_ttl=timedelta(seconds=settings.quote_ttl_seconds),
    )
    app.state.issuance = IssuanceContext(
        registry=registry, issuer=issuer_key, public_base_url=settings.public_base_url
    )

    install_error_handlers(app)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(discovery.router)
    app.include_router(quotes.router)
    app.include_router(payments.router)
    app.include_router(registrations.router)
    app.include_router(identities.router)
    return app
