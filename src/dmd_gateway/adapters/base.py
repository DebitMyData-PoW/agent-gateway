"""Common interface every payment adapter implements (DMD-01, X402-02, RAIL-02..04).

Adapters translate one payment rail into facts the gateway understands. They:
  * prepare a payment destination for an intent (unique address or reference, or a
    payment page of the payment provider);
  * independently observe the chain or payment provider and report what they see as a
    ``PaymentObservation`` (outputs, confirmations, conflicts).

Adapters do not decide payment statuses. The rail-independent rules in
``dmd_gateway.domain.settlement`` turn observations into the normalized statuses, so every
rail follows the same lifecycle.

Adapters never issue identities, never move treasury funds and never trust a transaction
reference supplied by the payer without checking it themselves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from dmd_gateway.domain.routes import RailConfig


class AdapterUnavailable(RuntimeError):
    """The rail's node or provider cannot be used right now (or is misconfigured).

    ``rate_limited``: the provider only refused because too many requests were sent (HTTP
    429), which usually passes by itself; the monitor alerts on it only when it lasts."""

    def __init__(self, message: str = "", *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass(frozen=True, slots=True)
class PaymentDestination:
    recipient: str
    # per_intent_address: the address itself identifies the intent.
    # shared_with_reference: a shared address plus a unique payment reference.
    # hosted_payment_page: the recipient is the address of a payment page made for this
    #   intent alone (a card payment page); the reference is the provider's ID for that page.
    recipient_kind: str
    payment_reference: str | None = None


@dataclass(frozen=True, slots=True)
class QuotedPayment:
    """What a quote asks for, given to the adapter before the quote is returned.

    Most rails only need the payment intent's ID; a card rail builds its payment page from
    the amount, the quote's end and the page the payer comes back to.
    """

    payment_intent_id: str
    registration_intent_id: str
    amount_base_units: int
    expires_at: datetime
    product_description: str
    return_url: str  # the Get a DID-LLM page; empty when the server has none


@dataclass(frozen=True, slots=True)
class SubmittedAuthorization:
    """A signed payment authorization the gateway submitted for settlement (x402).

    On rails with one shared receiving address, a transfer is linked to an intent only
    through the authorization the gateway itself received for that intent: the adapter
    looks for the on-chain use of exactly this (payer, nonce), never for transfers in
    general and never for references sent by a payer.
    """

    payer: str
    nonce: str
    submitted_at: datetime
    transaction_hint: str | None = None  # transaction reported by the facilitator
    search_from_block: int | None = None  # chain height just before submission


@dataclass(frozen=True, slots=True)
class ExpectedPayment:
    payment_intent_id: str
    network: str
    asset_canonical_id: str
    amount_base_units: int
    destination: PaymentDestination
    required_confirmations: int | None
    # Transactions seen in earlier observations, re-checked even if the node stops listing
    # them (for example after a double-spend).
    known_transactions: tuple[str, ...] = ()
    payer_reference_hint: str | None = None  # e.g. a txid the payer submitted; a hint only
    # x402 rails: the authorizations submitted for this intent.
    authorizations: tuple[SubmittedAuthorization, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservedOutput:
    """One verified output paying the expected destination."""

    transaction_reference: str
    output_index: int
    amount_base_units: int
    confirmations: int
    first_seen_at: datetime
    block_hash: str | None = None
    block_height: int | None = None


@dataclass(frozen=True, slots=True)
class PaymentObservation:
    payment_intent_id: str
    adapter: str
    observed_at: datetime
    # Outputs in transactions that are in the active chain or the mempool.
    outputs: tuple[ObservedOutput, ...] = ()
    # Transactions that paid us earlier but are now in conflict with the chain (double-spent).
    conflicted_transactions: tuple[str, ...] = ()
    # Transactions that were confirmed earlier but whose block left the active chain.
    reorged_transactions: tuple[str, ...] = ()
    tip_height: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Card rails: the payment provider gave the whole payment back to the card (a refund), or
    # the cardholder's bank opened a dispute (a chargeback). Chains never set these.
    refunded: bool = False
    disputed: bool = False

    @property
    def transaction_references(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(o.transaction_reference for o in self.outputs))


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

    def check_ready(self) -> None:  # noqa: B027 - optional hook
        """Verify the node/provider is reachable and safely configured; raise if not."""

    @abstractmethod
    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        """Return where the payer must send funds for this intent.

        Called inside the quote transaction, before the quote is returned (DMD-03).
        """

    def prepare_quote(self, quote: QuotedPayment) -> PaymentDestination:
        """Like ``prepare_destination``, with everything the quote asks for.

        Rails whose destination only depends on the intent (addresses, references) keep the
        default; a card rail creates its payment page here.
        """
        return self.prepare_destination(quote.payment_intent_id)

    @abstractmethod
    def observe(self, expected: ExpectedPayment) -> PaymentObservation:
        """Independently check the chain or provider and report what was received."""
