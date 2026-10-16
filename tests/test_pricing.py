from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from dmd_gateway.domain.routes import PricePolicy
from dmd_gateway.pricing import PriceUnavailable, select_price
from dmd_gateway.pricing.base import deviation_bps

from .fakes import FixedPriceSource

NOW = datetime.now(UTC)

POLICY = PricePolicy(
    source="primary",
    max_price_age_seconds=300,
    max_deviation_bps=200,
    fallback="refuse_quote",
)
WITH_SECONDARY = PricePolicy(
    source="primary",
    max_price_age_seconds=300,
    max_deviation_bps=200,
    fallback="secondary_source",
    secondary_source="secondary",
)


def _sources(**sources: FixedPriceSource) -> dict[str, FixedPriceSource]:
    return dict(sources)


def test_fresh_primary_price_is_used() -> None:
    sources = _sources(primary=FixedPriceSource("primary", "0.20"))
    observation = select_price(POLICY, "DMD", sources, NOW)
    assert observation.usd_per_unit == Decimal("0.20")
    assert observation.source == "primary"


def test_stale_price_is_refused() -> None:
    sources = _sources(primary=FixedPriceSource("primary", "0.20", age=timedelta(minutes=10)))
    with pytest.raises(PriceUnavailable):
        select_price(POLICY, "DMD", sources, NOW)


def test_future_dated_price_is_refused() -> None:
    sources = _sources(primary=FixedPriceSource("primary", "0.20", age=timedelta(minutes=-5)))
    with pytest.raises(PriceUnavailable):
        select_price(POLICY, "DMD", sources, NOW)


@pytest.mark.parametrize("price", ["0", "-1"])
def test_non_positive_price_is_refused(price: str) -> None:
    sources = _sources(primary=FixedPriceSource("primary", price))
    with pytest.raises(PriceUnavailable):
        select_price(POLICY, "DMD", sources, NOW)


def test_failing_source_with_refuse_fallback_refuses() -> None:
    sources = _sources(primary=FixedPriceSource("primary", "0.20", fail=True))
    with pytest.raises(PriceUnavailable):
        select_price(POLICY, "DMD", sources, NOW)


def test_secondary_is_used_when_primary_fails() -> None:
    sources = _sources(
        primary=FixedPriceSource("primary", "0.20", fail=True),
        secondary=FixedPriceSource("secondary", "0.21"),
    )
    assert select_price(WITH_SECONDARY, "DMD", sources, NOW).source == "secondary"


def test_sources_within_deviation_use_primary() -> None:
    sources = _sources(
        primary=FixedPriceSource("primary", "0.200"),
        secondary=FixedPriceSource("secondary", "0.203"),  # 150 bps apart
    )
    assert select_price(WITH_SECONDARY, "DMD", sources, NOW).source == "primary"


def test_sources_beyond_deviation_refuse() -> None:
    sources = _sources(
        primary=FixedPriceSource("primary", "0.20"),
        secondary=FixedPriceSource("secondary", "0.25"),  # 2500 bps apart
    )
    with pytest.raises(PriceUnavailable, match="disagree"):
        select_price(WITH_SECONDARY, "DMD", sources, NOW)


def test_unknown_source_refuses() -> None:
    with pytest.raises(PriceUnavailable):
        select_price(POLICY, "DMD", {}, NOW)


def test_incomplete_policy_refuses() -> None:
    sources = _sources(primary=FixedPriceSource("primary", "0.20"))
    with pytest.raises(PriceUnavailable):
        select_price(PricePolicy(source="primary"), "DMD", sources, NOW)


def test_deviation_bps() -> None:
    assert deviation_bps(Decimal("100"), Decimal("101")) == Decimal("100")
    assert deviation_bps(Decimal("101"), Decimal("100")) == Decimal("100")
