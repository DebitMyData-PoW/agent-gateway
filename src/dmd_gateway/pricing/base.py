"""Price sources and the rule that picks the price used in a quote (QUOTE-09, DEC-01).

The directive requires that a non-USD amount is computed only from an approved, signed,
monitored price-source policy, never from a front-end price, an unverified ticker or a
manually entered amount. A ``PriceSource`` implementation is responsible for verifying its
own data (signatures, authentication); ``select_price`` then applies the rail's policy:

  * the observation must be positive and no older than ``max_price_age_seconds``;
  * when a secondary source is configured and both answer, they must agree within
    ``max_deviation_bps`` or no quote is issued;
  * ``fallback: secondary_source`` uses the secondary source when the primary is down or
    stale; ``fallback: refuse_quote`` refuses the quote instead.

Any failure refuses the quote. A quote is never issued from a price that failed a check.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from dmd_gateway.domain.routes import PricePolicy

log = logging.getLogger(__name__)

# Tolerated clock difference for observations that appear to come from the future.
MAX_CLOCK_SKEW = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class PriceObservation:
    source: str
    asset_symbol: str
    usd_per_unit: Decimal
    observed_at: datetime


class PriceUnavailable(RuntimeError):
    """No acceptable price could be obtained; the quote must be refused."""


class PriceSource(ABC):
    """One approved price provider. Implementations must authenticate their data."""

    name: str

    @abstractmethod
    def get_usd_price(self, asset_symbol: str) -> PriceObservation:
        """Return the current USD price of one unit of ``asset_symbol``, or raise."""

    def bind(self, session_factory: Any) -> None:  # noqa: B027 (optional hook)
        """Called once the database is available, for sources that read stored prices."""


def _usable(
    source_name: str | None,
    asset_symbol: str,
    sources: Mapping[str, PriceSource],
    max_age: timedelta,
    now: datetime,
) -> PriceObservation | None:
    if not source_name or source_name not in sources:
        return None
    try:
        observation = sources[source_name].get_usd_price(asset_symbol)
    except Exception as exc:  # any provider failure means "no price from this source"
        log.warning(
            "price source failed",
            extra={"event": {"price_source": source_name, "exc_type": type(exc).__name__}},
        )
        return None
    if observation.asset_symbol != asset_symbol or observation.usd_per_unit <= 0:
        return None
    if observation.observed_at > now + MAX_CLOCK_SKEW:
        return None
    if now - observation.observed_at > max_age:
        return None
    return observation


def deviation_bps(a: Decimal, b: Decimal) -> Decimal:
    """Difference between two prices in basis points of the lower one."""
    low = min(a, b)
    return abs(a - b) / low * 10_000


def select_price(
    policy: PricePolicy,
    asset_symbol: str,
    sources: Mapping[str, PriceSource],
    now: datetime,
) -> PriceObservation:
    if policy.missing() or policy.max_price_age_seconds is None:
        raise PriceUnavailable("price policy is incomplete")
    max_age = timedelta(seconds=policy.max_price_age_seconds)

    primary = _usable(policy.source, asset_symbol, sources, max_age, now)
    secondary = _usable(policy.secondary_source, asset_symbol, sources, max_age, now)

    if primary and secondary:
        assert policy.max_deviation_bps is not None
        if deviation_bps(primary.usd_per_unit, secondary.usd_per_unit) > policy.max_deviation_bps:
            raise PriceUnavailable("price sources disagree beyond the permitted deviation")
        return primary
    if primary:
        return primary
    if secondary and policy.fallback == "secondary_source":
        return secondary
    raise PriceUnavailable("no fresh price is available from an approved source")
