"""Builds the parts shared by the API and the worker: route registry, adapters, price sources."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from dmd_gateway.adapters import AVAILABLE_ADAPTERS, PaymentAdapter
from dmd_gateway.domain.routes import (
    TEST_NETWORKS,
    PaymentRouteRegistry,
    RailState,
    RegistryError,
    load_registry,
)
from dmd_gateway.pricing import AVAILABLE_PRICE_SOURCES, PriceSource
from dmd_gateway.pricing.static_test import TEST_ONLY_PRICE_SOURCES
from dmd_gateway.settings import Settings

PriceSourceFactory = Callable[[Mapping[str, str]], PriceSource]


@dataclass(frozen=True)
class Runtime:
    registry: PaymentRouteRegistry
    adapters: Mapping[str, PaymentAdapter]  # rail id -> adapter, enabled rails only
    price_sources: Mapping[str, PriceSource]


def build_runtime(
    settings: Settings,
    environ: Mapping[str, str],
    *,
    adapters: Mapping[str, type[PaymentAdapter]] | None = None,
    price_sources: Mapping[str, PriceSourceFactory] | None = None,
) -> Runtime:
    adapter_classes = AVAILABLE_ADAPTERS if adapters is None else adapters
    if price_sources is None:
        price_sources = dict(AVAILABLE_PRICE_SOURCES)
        if settings.environment != "production":
            price_sources.update(TEST_ONLY_PRICE_SOURCES)

    registry = load_registry(
        settings.route_registry_path,
        environ=environ,
        available_adapters=adapter_classes.keys(),
        available_price_sources=price_sources.keys(),
    )

    enabled = {
        rail_id: rail
        for rail_id, rail in registry.rails.items()
        if registry.rail_state(rail_id) is RailState.ENABLED
    }
    for rail_id, rail in enabled.items():
        uses_test_price = {rail.price_policy.source, rail.price_policy.secondary_source} & set(
            TEST_ONLY_PRICE_SOURCES
        )
        if uses_test_price and rail.network.endswith(":mainnet"):
            raise RegistryError(f"rail {rail_id!r} uses a test-only price source on mainnet")
        if settings.environment == "production" and rail.network in TEST_NETWORKS:
            raise RegistryError(
                f"rail {rail_id!r} is on the test network {rail.network}; "
                "production only accepts real networks"
            )

    return Runtime(
        registry=registry,
        adapters={
            rail_id: adapter_classes[rail.adapter](rail_id, rail, environ)
            for rail_id, rail in enabled.items()
        },
        price_sources={name: factory(environ) for name, factory in price_sources.items()},
    )
