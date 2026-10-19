"""Test doubles for payment adapters and price sources (never used outside tests)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dmd_gateway.adapters.base import (
    ExpectedPayment,
    ObservedOutput,
    PaymentAdapter,
    PaymentDestination,
    PaymentObservation,
)
from dmd_gateway.identity.crypto import encode_signature
from dmd_gateway.pricing import PriceObservation, PriceSource

# A fixed agent controller key (RFC 8032 test vector 1) so that tests are deterministic.
AGENT_KEY = Ed25519PrivateKey.from_private_bytes(
    bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
)
AGENT_PUBLIC_HEX = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
AGENT_CONTROLLER = "ed25519:z6MktwupdmLXVVqTzCw4i46r4uGyosGXRnR3XjN4Zq7oMMsw"


def sign_challenge(message: str, key: Ed25519PrivateKey = AGENT_KEY) -> str:
    """What the agent does with a controller challenge: sign the exact message bytes."""
    return encode_signature(key.sign(message.encode("utf-8")))


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

    def observe(self, expected: ExpectedPayment) -> PaymentObservation:
        return SCRIPTED.observation(expected, self.name)


class FakeX402Adapter(PaymentAdapter):
    """Shared merchant address from the environment plus a unique reference."""

    name = "X402PaymentAdapter"

    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        return PaymentDestination(
            recipient=self.configured_recipient(),
            recipient_kind="shared_with_reference",
            payment_reference=payment_intent_id,
        )

    def observe(self, expected: ExpectedPayment) -> PaymentObservation:
        return SCRIPTED.observation(expected, self.name)


class ScriptedChain:
    """What the fake adapters 'see' on chain, set by each test per payment intent."""

    def __init__(self) -> None:
        self.outputs: dict[str, list[ObservedOutput]] = {}
        self.conflicted: dict[str, list[str]] = {}
        self.unavailable = False

    def reset(self) -> None:
        self.__init__()

    def pay(
        self,
        payment_intent_id: str,
        units: int,
        *,
        txid: str,
        vout: int = 0,
        confirmations: int = 0,
        first_seen_at: datetime | None = None,
    ) -> None:
        outputs = [
            o for o in self.outputs.get(payment_intent_id, []) if o.transaction_reference != txid
        ]
        outputs.append(
            ObservedOutput(
                transaction_reference=txid,
                output_index=vout,
                amount_base_units=units,
                confirmations=confirmations,
                first_seen_at=first_seen_at or datetime.now(UTC),
                block_hash="ab" * 32 if confirmations else None,
                block_height=100 if confirmations else None,
            )
        )
        self.outputs[payment_intent_id] = outputs

    def confirm(self, payment_intent_id: str, confirmations: int) -> None:
        self.outputs[payment_intent_id] = [
            ObservedOutput(
                o.transaction_reference,
                o.output_index,
                o.amount_base_units,
                confirmations,
                o.first_seen_at,
                "ab" * 32 if confirmations else None,
                100 if confirmations else None,
            )
            for o in self.outputs.get(payment_intent_id, [])
        ]

    def double_spend(self, payment_intent_id: str) -> None:
        txids = [o.transaction_reference for o in self.outputs.pop(payment_intent_id, [])]
        self.conflicted[payment_intent_id] = txids

    def observation(self, expected: ExpectedPayment, adapter: str) -> PaymentObservation:
        if self.unavailable:
            from dmd_gateway.adapters.base import AdapterUnavailable

            raise AdapterUnavailable("scripted outage")
        return PaymentObservation(
            payment_intent_id=expected.payment_intent_id,
            adapter=adapter,
            observed_at=datetime.now(UTC),
            outputs=tuple(self.outputs.get(expected.payment_intent_id, [])),
            conflicted_transactions=tuple(self.conflicted.get(expected.payment_intent_id, [])),
        )


SCRIPTED = ScriptedChain()


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
