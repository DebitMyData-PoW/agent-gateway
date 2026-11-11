"""Card payment adapter: Stripe Checkout (a payment page hosted by Stripe).

How a card payment works:
  * The quote creates a Checkout Session for exactly the quoted amount in US cents, with the
    payment intent's ID in its metadata and as its ``client_reference_id``. The session's
    page (``url``) is where the payer enters a card: card details go to Stripe only, never to
    DebitMyData. The page closes when the quote ends (``expires_at``), so no card can be
    charged for an expired quote.
  * The worker reads the session back from Stripe (with its PaymentIntent and Charge) and
    reports the charge as one output once Stripe says it is paid, after checking that the
    session, the PaymentIntent and the charge all belong to this payment intent. A card
    payment is final when it succeeds, so it counts as one confirmation.
  * Settled payments stay watched for a while (``watch_after_settlement_hours``): a full
    refund or a dispute (chargeback) is reported, and the settlement rules move the payment
    to ``refunded`` or ``disputed``.

The key is a restricted Stripe key: it can create and read Checkout Sessions and read
PaymentIntents and Charges. It cannot refund, pay out or change the account; refunds are
made by a person, in Stripe's dashboard.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.parse import urlencode, urlsplit

from dmd_gateway.adapters.base import (
    AdapterUnavailable,
    ExpectedPayment,
    ObservedOutput,
    PaymentAdapter,
    PaymentDestination,
    PaymentObservation,
    QuotedPayment,
)
from dmd_gateway.adapters.stripe_api import StripeClient, StripeUnavailable
from dmd_gateway.domain.payment_status import HOSTED_PAYMENT_PAGE
from dmd_gateway.domain.routes import STRIPE_CHECKOUT, STRIPE_NETWORKS, RailConfig

CURRENCY = "usd"
SESSIONS = "/v1/checkout/sessions"
# What the payer's browser gets back from Stripe's page, on the Get a DID-LLM page.
RETURN_PAID = "paid"
RETURN_BACK = "back"

T = TypeVar("T")


def return_link(page: str, outcome: str, payment_intent_id: str) -> str:
    """The Get a DID-LLM page, told which payment the payer comes back from."""
    query = urlencode({"card": outcome, "payment": payment_intent_id})
    return f"{page}{'&' if '?' in page else '?'}{query}"


class StripeCheckoutAdapter(PaymentAdapter):
    name = "StripeCheckoutAdapter"

    def __init__(
        self,
        rail_id: str,
        rail: RailConfig,
        environ: Mapping[str, str],
        *,
        client: StripeClient | None = None,
    ) -> None:
        super().__init__(rail_id, rail, environ)
        if (
            rail.protocol != STRIPE_CHECKOUT
            or rail.checkout is None
            or rail.network not in STRIPE_NETWORKS
        ):
            raise ValueError(f"rail {rail_id} is not a complete Stripe Checkout rail")
        self.settings = rail.checkout
        self.live = rail.network == "stripe:live"
        self._client = client
        self._ready = False

    # ------------------------------------------------------------------ Stripe

    def client(self) -> StripeClient:
        if self._client is None:
            key = (self.environ.get(self.settings.secret_key_env) or "").strip()
            mode = "live" if self.live else "test"
            if not key.startswith(f"rk_{mode}_"):
                # Never says more about the key than its variable's name.
                raise AdapterUnavailable(
                    f"{self.settings.secret_key_env} is not a restricted {mode}-mode Stripe key"
                )
            self._client = StripeClient(key)
        return self._client

    def _stripe(self, call: Callable[[StripeClient], T]) -> T:
        try:
            return call(self.client())
        except StripeUnavailable as exc:
            raise AdapterUnavailable(str(exc), rate_limited=exc.rate_limited) from exc

    def check_ready(self) -> None:
        # The three reads the worker needs: sessions, their PaymentIntents and Charges.
        for path in (SESSIONS, "/v1/payment_intents", "/v1/charges"):
            answer = self._stripe(lambda stripe, path=path: stripe.get(path, {"limit": 1}))
            if answer.get("object") != "list":
                raise AdapterUnavailable(f"unexpected answer from Stripe to GET {path}")
        self._ready = True

    def _ensure_ready(self) -> None:
        if not self._ready:
            self.check_ready()

    # ------------------------------------------------------------------ quotes

    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        raise AdapterUnavailable("a card payment page is made from the whole quote")

    def prepare_quote(self, quote: QuotedPayment) -> PaymentDestination:
        if not quote.return_url:
            raise AdapterUnavailable("no page for payers to come back to (the Get a DID-LLM page)")
        self._ensure_ready()
        reference = {
            "payment_intent_id": quote.payment_intent_id,
            "registration_intent_id": quote.registration_intent_id,
        }
        params: dict[str, Any] = {
            "mode": "payment",
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": CURRENCY,
                        "unit_amount": quote.amount_base_units,
                        "product_data": {
                            "name": self.settings.product_name,
                            "description": quote.product_description,
                        },
                    },
                }
            ],
            # Cards only (Apple Pay and Google Pay are cards too): paid at once, never days
            # later like a bank debit.
            "allowed_payment_method_types": ["card"],
            "client_reference_id": quote.payment_intent_id,
            "metadata": reference,
            "payment_intent_data": {
                "description": f"{self.settings.product_name} ({quote.registration_intent_id})",
                "metadata": reference,
            },
            "expires_at": int(quote.expires_at.timestamp()),
            "submit_type": "pay",
            "success_url": return_link(quote.return_url, RETURN_PAID, quote.payment_intent_id),
            "cancel_url": return_link(quote.return_url, RETURN_BACK, quote.payment_intent_id),
        }
        session = self._stripe(
            lambda stripe: stripe.post(
                SESSIONS, params, idempotency_key=f"dmd-checkout-{quote.payment_intent_id}"
            )
        )
        session_id = str(session.get("id") or "")
        url = str(session.get("url") or "")
        problems = []
        if not session_id.startswith("cs_"):
            problems.append("no Checkout Session ID")
        if urlsplit(url).scheme != "https":
            problems.append("no https payment page")
        if session.get("livemode") is not self.live:
            problems.append("the session is in the other mode (live/test) than this rail")
        if session.get("amount_total") != quote.amount_base_units:
            problems.append("the session's total is not the quoted amount")
        if str(session.get("currency") or "").lower() != CURRENCY:
            problems.append("the session is not in US dollars")
        if problems:
            raise AdapterUnavailable(
                "Stripe's new Checkout Session is unusable: " + "; ".join(problems)
            )
        return PaymentDestination(
            recipient=url, recipient_kind=HOSTED_PAYMENT_PAGE, payment_reference=session_id
        )

    # ------------------------------------------------------------------ observation

    def observe(self, expected: ExpectedPayment) -> PaymentObservation:
        now = datetime.now(UTC)
        session_id = expected.destination.payment_reference or ""
        if expected.destination.recipient_kind != HOSTED_PAYMENT_PAGE or not session_id:
            raise AdapterUnavailable(f"{expected.payment_intent_id} has no card payment page")
        session = self._stripe(
            lambda stripe: stripe.get(
                f"{SESSIONS}/{session_id}", {"expand": ["payment_intent.latest_charge"]}
            )
        )
        self._check_belongs(session, session_id, expected.payment_intent_id, "Checkout Session")
        if session.get("payment_status") != "paid":
            note = f"{session_id}: payment page {session.get('status') or 'unknown'}, not paid"
            return PaymentObservation(expected.payment_intent_id, self.name, now, notes=(note,))

        intent = session.get("payment_intent")
        charge = intent.get("latest_charge") if isinstance(intent, dict) else None
        if not isinstance(intent, dict) or not isinstance(charge, dict):
            raise AdapterUnavailable(f"{session_id} is paid but Stripe did not show its charge")
        self._check_belongs(
            intent, str(intent.get("id") or ""), expected.payment_intent_id, "PaymentIntent"
        )
        if charge.get("payment_intent") != intent.get("id"):
            raise AdapterUnavailable(f"{session_id}: the charge is not the session's payment")
        problems = []
        if intent.get("status") != "succeeded":
            problems.append(f"PaymentIntent {intent.get('status')}")
        if charge.get("status") != "succeeded" or charge.get("paid") is not True:
            problems.append(f"charge {charge.get('status')}")
        if charge.get("captured") is not True:
            problems.append("charge not captured")
        for name, item in (("PaymentIntent", intent), ("charge", charge)):
            if str(item.get("currency") or "").lower() != CURRENCY:
                problems.append(f"{name} not in US dollars")
            if item.get("livemode") is not self.live:
                problems.append(f"{name} in the other mode (live/test)")
        if problems:
            # Paid on Stripe's side but not as expected: nothing is credited, a person checks.
            raise AdapterUnavailable(
                f"{session_id}: unexpected card payment ({', '.join(problems)})"
            )

        amount = int(charge.get("amount_captured") or charge.get("amount") or 0)
        refunded_units = int(charge.get("amount_refunded") or 0)
        notes = []
        if refunded_units and not charge.get("refunded"):
            notes.append(f"{intent['id']}: {refunded_units} cents refunded (partial refund)")
        output = ObservedOutput(
            transaction_reference=str(intent["id"]),
            output_index=0,
            amount_base_units=amount,
            confirmations=1,  # a card payment is final when it succeeds
            first_seen_at=datetime.fromtimestamp(int(charge["created"]), UTC),
        )
        return PaymentObservation(
            payment_intent_id=expected.payment_intent_id,
            adapter=self.name,
            observed_at=now,
            outputs=(output,) if amount > 0 else (),
            notes=tuple(notes),
            refunded=charge.get("refunded") is True,
            disputed=charge.get("disputed") is True,
        )

    def _check_belongs(
        self, item: Mapping[str, Any], item_id: str, payment_intent_id: str, kind: str
    ) -> None:
        """A Stripe object counts only if it carries this payment intent's own ID."""
        metadata = item.get("metadata") or {}
        if (
            not item_id
            or not isinstance(metadata, dict)
            or metadata.get("payment_intent_id") != payment_intent_id
        ):
            raise AdapterUnavailable(f"{kind} {item_id or '?'} is not for {payment_intent_id}")
        if kind == "Checkout Session" and (
            item.get("id") != item_id or item.get("client_reference_id") != payment_intent_id
        ):
            raise AdapterUnavailable(f"{kind} {item_id} is not for {payment_intent_id}")
        if item.get("livemode") is not self.live:
            raise AdapterUnavailable(f"{kind} {item_id} is in the other mode (live/test)")
