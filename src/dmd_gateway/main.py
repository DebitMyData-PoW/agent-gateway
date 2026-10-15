"""Application factory.

Run locally with:
    uvicorn --factory dmd_gateway.main:create_app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine

from dmd_gateway import __version__
from dmd_gateway.adapters import AVAILABLE_ADAPTERS
from dmd_gateway.api import discovery, health
from dmd_gateway.db.session import build_engine
from dmd_gateway.domain.routes import RailState, load_registry
from dmd_gateway.observability import CorrelationIdMiddleware, configure_logging
from dmd_gateway.settings import Settings, get_settings, runtime_environ

log = logging.getLogger("dmd_gateway")


def create_app(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    registry = load_registry(
        settings.route_registry_path,
        environ=runtime_environ() if environ is None else environ,
        available_adapters=AVAILABLE_ADAPTERS.keys(),
    )
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
            "payment verification and DebitMyData identity issuance."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine

    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(discovery.router)
    return app
