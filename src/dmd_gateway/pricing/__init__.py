"""Approved price sources.

``AVAILABLE_PRICE_SOURCES`` lists the price sources that are actually implemented, keyed by
the name used in ``price_policy.source``. It is empty until the DMD/USD source is approved
(DEC-01), so no non-USD rail can be enabled yet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from dmd_gateway.pricing.base import (
    PriceObservation,
    PriceSource,
    PriceUnavailable,
    select_price,
)

# name -> factory taking the runtime environment (for API keys etc.)
AVAILABLE_PRICE_SOURCES: dict[str, Callable[[Mapping[str, str]], PriceSource]] = {}

__all__ = [
    "AVAILABLE_PRICE_SOURCES",
    "PriceObservation",
    "PriceSource",
    "PriceUnavailable",
    "select_price",
]
