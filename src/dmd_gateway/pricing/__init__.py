"""Approved price sources.

``AVAILABLE_PRICE_SOURCES`` lists the price sources that are actually implemented, keyed by
the name used in ``price_policy.source``. ``dmd_mining_cost`` is the approved interim DMD/USD
source (DEC-01): the cost of mining DMD, until DMD has a market price.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from dmd_gateway.pricing.base import (
    PriceObservation,
    PriceSource,
    PriceUnavailable,
    select_price,
)
from dmd_gateway.pricing.mining_cost import StoredMiningCostPrice

# name -> factory taking the runtime environment (for API keys etc.)
AVAILABLE_PRICE_SOURCES: dict[str, Callable[[Mapping[str, str]], PriceSource]] = {
    StoredMiningCostPrice.name: StoredMiningCostPrice,
}

__all__ = [
    "AVAILABLE_PRICE_SOURCES",
    "PriceObservation",
    "PriceSource",
    "PriceUnavailable",
    "select_price",
]
