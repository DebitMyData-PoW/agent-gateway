"""Application factory.

Run locally with:
    uvicorn --factory dmd_gateway.main:create_app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy import Engine

from dmd_gateway import __version__
from dmd_gateway.adapters import AVAILABLE_ADAPTERS, PaymentAdapter
from dmd_gateway.api import discovery, health, quotes
from dmd_gateway.db.session import build_engine, session_factory
from dmd_gateway.domain.routes import RailState, load_registry
from dmd_gateway.errors import install_error_handlers
from dmd_gateway.observability import CorrelationIdMiddleware, configure_logging
from dmd_gateway.pricing import AVAILABLE_PRICE_SOURCES, PriceSource
from dmd_gateway.services.quotes import QuoteContext
from dmd_gateway.settings import Settings, get_settings, runtime_environ

log = logging.getLogger("dmd_gateway")

PriceSourceFactory = Callable[[Mapping[str, str]], PriceSource]


def create_app(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    engine: Engine | None = None,
    adapters: Mapping[str, type[PaymentAdapter]] | None = None,
    price_sources: Mapping[str, PriceSourceFactory] | None = None,
) -> FastAPI:
    """Build the application.

    ``adapters`` and ``price_sources`` default to the implemented ones; tests pass their own.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    env = runtime_environ() if environ is None else environ
    adapter_classes = AVAILABLE_ADAPTERS if adapters is None else adapters
    source_factories = AVAILABLE_PRICE_SOURCES if price_sources is None else price_sources

    registry = load_registry(
        settings.route_registry_path,
        environ=env,
        available_adapters=adapter_classes.keys(),
        available_price_sources=source_factories.keys(),
    )
    rail_adapters = {
        rail_id: adapter_classes[rail.adapter](rail_id, rail, env)
        for rail_id, rail in registry.rails.items()
        if registry.rail_state(rail_id) is RailState.ENABLED
    }
    sources = {name: factory(env) for name, factory in source_factories.items()}
    engine = engine or build_engine(settings)

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
            "payment verification and DebitMyData DID issuance. In development: payment "
            "options appear only once their payment rail is enabled."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    app.state.quote_context = QuoteContext(
        registry=registry,
        adapters=rail_adapters,
        price_sources=sources,
        quote_ttl=timedelta(seconds=settings.quote_ttl_seconds),
    )

    install_error_handlers(app)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(discovery.router)
    app.include_router(quotes.router)
    return app
