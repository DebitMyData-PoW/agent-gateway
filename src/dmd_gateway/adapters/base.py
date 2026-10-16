"""Common interface every payment adapter implements (DMD-01, X402-02, RAIL-02..04).

Adapters translate one payment rail into the gateway's normalized model. They:
  * prepare a payment destination for an intent (unique address or reference);
  * independently observe the chain / payment provider and report a ``SettlementObservation``.

Adapters never issue identities, never move treasury funds and never trust a transaction
reference supplied by the payer without checking it themselves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.domain.routes import RailConfig


@dataclass(frozen=True, slots=True)
class PaymentDestination:
    recipient: str
    # per_intent_address: the address itself identifies the intent.
    # shared_with_reference: a shared address plus a unique payment reference.
    recipient_kind: str
    payment_reference: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectedPayment:
    payment_intent_id: str
    network: str
    asset_canonical_id: str
    amount_base_units: int
    destination: PaymentDestination
    required_confirmations: int | None
    payer_reference_hint: str | None = None  # e.g. a txid the payer submitted; a hint only


@dataclass(frozen=True, slots=True)
class SettlementObservation:
    payment_intent_id: str
    adapter: str
    status: PaymentStatus
    observed_at: datetime
    transaction_reference: str | None = None
    output_index: int | None = None
    observed_amount_base_units: int | None = None
    confirmations: int | None = None
    required_confirmations: int | None = None
    block_hash: str | None = None
    block_height: int | None = None
    reorg_safe: bool = False


class PaymentAdapter(ABC):
    """Base class for rail-specific adapters.

    One instance is created per enabled rail at startup, with that rail's configuration and
    the runtime environment (for receiving addresses and service credentials).
    """

    name: str

    def __init__(self, rail_id: str, rail: RailConfig, environ: Mapping[str, str]) -> None:
        self.rail_id = rail_id
        self.rail = rail
        self.environ = environ

    def configured_recipient(self) -> str:
        """The merchant address for rails whose recipient comes from the environment."""
        if self.rail.recipient.source != "env" or not self.rail.recipient.env:
            raise RuntimeError(f"rail {self.rail_id} has no environment-configured recipient")
        value = (self.environ.get(self.rail.recipient.env) or "").strip()
        if not value:
            raise RuntimeError(f"{self.rail.recipient.env} is empty")
        return value

    @abstractmethod
    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        """Return where the payer must send funds for this intent.

        Called inside the quote transaction, before the quote is returned (DMD-03).
        """

    @abstractmethod
    def observe(self, expected: ExpectedPayment) -> SettlementObservation:
        """Independently check the chain or provider and report the normalized status."""
