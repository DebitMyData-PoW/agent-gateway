"""Test doubles for payment adapters and price sources (never used outside tests)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from dmd_gateway.adapters.base import (
    ExpectedPayment,
    PaymentAdapter,
    PaymentDestination,
    SettlementObservation,
)
from dmd_gateway.pricing import PriceObservation, PriceSource


class FakeDmdAdapter(PaymentAdapter):
    """Hands out a unique DMD address per intent, like a watch-only descriptor wallet would."""

    name = "DMDNativePaymentAdapter"

    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        suffix = payment_intent_id.rsplit("_", 1)[-1].lower()
        return PaymentDestination(
            recipient=f"dm1qtest{suffix}",
            recipient_kind="per_intent_address",
            payment_reference=payment_intent_id,
        )

    def observe(self, expected: ExpectedPayment) -> SettlementObservation:
        raise NotImplementedError


class FakeX402Adapter(PaymentAdapter):
    """Shared merchant address from the environment plus a unique reference."""

    name = "X402PaymentAdapter"

    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        return PaymentDestination(
            recipient=self.configured_recipient(),
            recipient_kind="shared_with_reference",
            payment_reference=payment_intent_id,
        )

    def observe(self, expected: ExpectedPayment) -> SettlementObservation:
        raise NotImplementedError


class FixedPriceSource(PriceSource):
    def __init__(
        self,
        name: str,
        usd_per_unit: str,
        *,
        age: timedelta = timedelta(seconds=5),
        fail: bool = False,
    ) -> None:
        self.name = name
        self.usd_per_unit = Decimal(usd_per_unit)
        self.age = age
        self.fail = fail

    def get_usd_price(self, asset_symbol: str) -> PriceObservation:
        if self.fail:
            raise ConnectionError("price source down")
        return PriceObservation(
            source=self.name,
            asset_symbol=asset_symbol,
            usd_per_unit=self.usd_per_unit,
            observed_at=datetime.now(UTC) - self.age,
        )


def fixed_price_factory(
    name: str, usd_per_unit: str, **kwargs: object
) -> Callable[[Mapping[str, str]], PriceSource]:
    return lambda _environ: FixedPriceSource(name, usd_per_unit, **kwargs)  # type: ignore[arg-type]
