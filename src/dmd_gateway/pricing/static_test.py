"""A fixed DMD/USD price for regtest, testnet and demos. Never available in production.

The directive forbids manually entered prices as the production settlement authority, so
this source is only registered when ``DMD_GW_ENVIRONMENT`` is not ``production``, and the
gateway refuses to start if a mainnet rail uses it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from dmd_gateway.domain.money import AmountError, parse_amount
from dmd_gateway.pricing.base import PriceObservation, PriceSource, PriceUnavailable

ENV_PRICE = "DMD_STATIC_TEST_PRICE_USD"


class StaticTestPriceSource(PriceSource):
    name = "static_test_price"

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._raw = (environ.get(ENV_PRICE) or "").strip()

    def get_usd_price(self, asset_symbol: str) -> PriceObservation:
        if not self._raw:
            raise PriceUnavailable(f"{ENV_PRICE} is not set")
        try:
            price = parse_amount(self._raw)
        except AmountError as exc:
            raise PriceUnavailable(f"{ENV_PRICE} is not a valid amount") from exc
        if price <= 0:
            raise PriceUnavailable(f"{ENV_PRICE} must be greater than zero")
        return PriceObservation(
            source=self.name,
            asset_symbol=asset_symbol,
            usd_per_unit=price,
            observed_at=datetime.now(UTC),
        )


TEST_ONLY_PRICE_SOURCES = {StaticTestPriceSource.name: StaticTestPriceSource}
