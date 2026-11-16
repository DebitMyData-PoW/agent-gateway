"""PayPal payment adapter: PayPal Checkout (Orders v2, a payment page hosted by PayPal).

How a PayPal payment works:
  * The quote creates a PayPal order for exactly the quoted amount in US dollars, with the
    payment intent's ID as its reference, ``custom_id`` and ``invoice_id`` (PayPal refuses a
    second payment with the same invoice ID). Its page (``payer-action`` link) is where a
    person pays, with a PayPal account or a card through PayPal: what they pay with goes to
    PayPal only, never to DebitMyData. Payments that take days (eCheck) are refused
    (``IMMEDIATE_PAYMENT_REQUIRED``).
  * Once the person approves the payment on PayPal's page, the order is ``APPROVED``: no
    money has moved yet. The worker reads the order back and captures it (takes the payment)
    only while the quote is still open: an order approved after the quote ended is never
    captured, so nobody pays for an expired quote. The capture repeats safely
    (``PayPal-Request-Id``).
  * A completed capture of exactly the quoted amount, for this payment intent, is reported
    as one output with one confirmation (a PayPal payment is final when it completes). A
    capture PayPal holds (``PENDING``: a review, an account setting) is reported once PayPal
    completes it.
  * Settled payments stay watched for a while (``watch_after_settlement_hours``): a full
    refund, or a dispute or chargeback, is reported, and the settlement rules move the
    payment to ``refunded`` or ``disputed``.

The credentials are the client ID and secret of DebitMyData's PayPal REST app (a sandbox
app on a sandbox rail). Refunds are made by a person, in PayPal's dashboard.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
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
from dmd_gateway.adapters.paypal_api import PayPalClient, PayPalUnavailable
from dmd_gateway.domain.payment_status import HOSTED_PAYMENT_PAGE
from dmd_gateway.domain.routes import PAYPAL_CHECKOUT, PAYPAL_NETWORKS, RailConfig

log = logging.getLogger(__name__)

CURRENCY = "USD"
ORDERS = "/v2/checkout/orders"
DISPUTES = "/v1/customer/disputes"
DISPUTES_SCOPE = "https://uri.paypal.com/services/disputes/read-seller"
BRAND_NAME = "DebitMyData"
# What the payer's browser gets back from PayPal's page, on the Get a DID-LLM page (PayPal
# adds its own token and PayerID to it).
RETURN_PAID = "paid"
RETURN_BACK = "back"
# An approved order is captured only if the quote is still open this long after: the
# capture's time is when the payment counts, and it must be before the quote ends.
CAPTURE_MARGIN = timedelta(seconds=30)
# PayPal's page for a payer, on its own site only.
PAYER_PAGE_HOSTS = {True: "www.paypal.com", False: "www.sandbox.paypal.com"}
ORDER_ID = re.compile(r"^[A-Z0-9]{8,40}$")
# Why PayPal holds a capture because of the payer's bank or a complaint: the money came in,
# and is being claimed back (a dispute).
DISPUTE_REASONS = frozenset({"BUYER_COMPLAINT", "CHARGEBACK"})
# Capture answers where PayPal refuses this payment as the payer approved it (what they chose
# to pay with, or PayPal's own checks), or finds the order not approved (the payer is on
# PayPal's page again). PayPal counts every capture attempt and refuses an order for good after
# too many (MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED): a refused capture is tried again only
# after a wait that doubles each time (1, 2, 4, 8 minutes), at most MAX_CAPTURE_ATTEMPTS times
# for one order. Meanwhile the payer can open PayPal's page again and choose another way to
# pay.
PAYER_AGAIN = frozenset(
    {
        "INSTRUMENT_DECLINED",
        "PAYER_ACTION_REQUIRED",
        "TRANSACTION_REFUSED",
        "PAYER_CANNOT_PAY",
        "MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED",
        "ORDER_NOT_APPROVED",
    }
)
MAX_CAPTURE_ATTEMPTS = 5
RETRY_AFTER_SECONDS = 60

T = TypeVar("T")


def return_link(page: str, outcome: str, payment_intent_id: str) -> str:
    """The Get a DID-LLM page, told which PayPal payment the payer comes back from."""
    query = urlencode({"paypal": outcome, "payment": payment_intent_id})
    return f"{page}{'&' if '?' in page else '?'}{query}"


def usd_text(cents: int) -> str:
    """Cents as PayPal writes an amount: ``2500`` is ``25.00``."""
    return f"{cents // 100}.{cents % 100:02d}"


def usd_cents(value: Any) -> int | None:
    """PayPal's amount text in cents, or None if it is not a whole number of cents."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    cents = amount * 100
    if not amount.is_finite() or cents != cents.to_integral_value() or amount < 0:
        return None
    return int(cents)


class PayPalCheckoutAdapter(PaymentAdapter):
    name = "PayPalCheckoutAdapter"

    def __init__(
        self,
        rail_id: str,
        rail: RailConfig,
        environ: Mapping[str, str],
        *,
        client: PayPalClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(rail_id, rail, environ)
        if (
            rail.protocol != PAYPAL_CHECKOUT
            or rail.checkout is None
            or rail.checkout.provider != "paypal"
            or rail.network not in PAYPAL_NETWORKS
        ):
            raise ValueError(f"rail {rail_id} is not a complete PayPal Checkout rail")
        self.settings = rail.checkout
        self.live = rail.network == "paypal:live"
        self._client = client
        self._ready = False
        self._clock = clock
        # Refused captures: order ID -> (how many, when the next try may be).
        self._refused: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------ PayPal

    def client(self) -> PayPalClient:
        if self._client is None:
            client_id = (self.environ.get(self.settings.client_id_env or "") or "").strip()
            secret = (self.environ.get(self.settings.secret_key_env) or "").strip()
            if not client_id or not secret:
                # Never says more about the credentials than their variables' names.
                raise AdapterUnavailable(
                    f"{self.settings.client_id_env} or {self.settings.secret_key_env} is empty"
                )
            self._client = PayPalClient(client_id, secret, live=self.live)
        return self._client

    def _paypal(self, call: Callable[[PayPalClient], T]) -> T:
        try:
            return call(self.client())
        except PayPalUnavailable as exc:
            raise AdapterUnavailable(str(exc), rate_limited=exc.rate_limited) from exc

    def check_ready(self) -> None:
        # The app's credentials work, for this rail's PayPal (live or sandbox).
        self._paypal(lambda paypal: paypal.access_token())
        self._ready = True

    def _ensure_ready(self) -> None:
        if not self._ready:
            self.check_ready()

    # ------------------------------------------------------------------ quotes

    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        raise AdapterUnavailable("a PayPal payment page is made from the whole quote")

    def prepare_quote(self, quote: QuotedPayment) -> PaymentDestination:
        if not quote.return_url:
            raise AdapterUnavailable("no page for payers to come back to (the Get a DID-LLM page)")
        self._ensure_ready()
        payment_id = quote.payment_intent_id
        description = f"{self.settings.product_name}: {quote.product_description}"
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": payment_id,
                    "custom_id": payment_id,
                    "invoice_id": payment_id,
                    "description": description[:127],
                    "amount": {
                        "currency_code": CURRENCY,
                        "value": usd_text(quote.amount_base_units),
                    },
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": BRAND_NAME,
                        "shipping_preference": "NO_SHIPPING",
                        "user_action": "PAY_NOW",
                        # Paid at once, never days later (eCheck).
                        "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                        "return_url": return_link(quote.return_url, RETURN_PAID, payment_id),
                        "cancel_url": return_link(quote.return_url, RETURN_BACK, payment_id),
                    }
                }
            },
        }
        order = self._paypal(
            lambda paypal: paypal.post(ORDERS, body, request_id=f"dmd-order-{payment_id}")
        )
        order_id = str(order.get("id") or "")
        page = self._payer_page(order)
        problems = []
        if not ORDER_ID.match(order_id):
            problems.append("no order ID")
        if order.get("status") not in ("PAYER_ACTION_REQUIRED", "CREATED"):
            problems.append(f"status {order.get('status')}")
        if page is None:
            problems.append(f"no payment page on {PAYER_PAGE_HOSTS[self.live]}")
        try:
            self._check_belongs(order, order_id, payment_id, quote.amount_base_units)
        except AdapterUnavailable as exc:
            problems.append(str(exc))
        if problems or page is None:
            raise AdapterUnavailable("PayPal's new order is unusable: " + "; ".join(problems))
        return PaymentDestination(
            recipient=page, recipient_kind=HOSTED_PAYMENT_PAGE, payment_reference=order_id
        )

    def _payer_page(self, order: Mapping[str, Any]) -> str | None:
        """The order's page for the payer, on PayPal's own site (live or sandbox)."""
        for link in order.get("links") or []:
            if not isinstance(link, dict) or link.get("rel") not in ("payer-action", "approve"):
                continue
            href = str(link.get("href") or "")
            parts = urlsplit(href)
            if parts.scheme == "https" and parts.hostname == PAYER_PAGE_HOSTS[self.live]:
                return href
        return None

    # ------------------------------------------------------------------ observation

    def observe(self, expected: ExpectedPayment) -> PaymentObservation:
        now = datetime.now(UTC)
        payment_id = expected.payment_intent_id
        order_id = expected.destination.payment_reference or ""
        if expected.destination.recipient_kind != HOSTED_PAYMENT_PAGE or not order_id:
            raise AdapterUnavailable(f"{payment_id} has no PayPal payment page")
        try:
            order = self._order(order_id, expected)
        except AdapterUnavailable as exc:
            cause = exc.__cause__
            if (
                isinstance(cause, PayPalUnavailable)
                and cause.status == 404
                and not expected.known_transactions
            ):
                # PayPal has no such order for this app: made with other credentials (the
                # app was replaced). Nobody can take it for DebitMyData, so nothing is paid,
                # and the payment expires with its quote.
                note = f"{order_id}: PayPal knows no such order for this app: nothing taken"
                return PaymentObservation(payment_id, self.name, now, notes=(note,))
            raise

        if order.get("status") == "APPROVED":
            ends = expected.quote_expires_at
            if ends is None or now + CAPTURE_MARGIN > ends:
                note = f"{order_id}: approved after the quote ended, not captured: nothing taken"
                return PaymentObservation(payment_id, self.name, now, notes=(note,))
            refused, next_try = self._refused.get(order_id, (0, 0.0))
            if refused >= MAX_CAPTURE_ATTEMPTS:
                note = (
                    f"{order_id}: refused by PayPal {refused} times, not tried again: nothing taken"
                )
                return PaymentObservation(payment_id, self.name, now, notes=(note,))
            if self._clock() < next_try:
                wait = int(next_try - self._clock()) + 1
                note = f"{order_id}: refused by PayPal; tried again in {wait} s at the earliest"
                return PaymentObservation(payment_id, self.name, now, notes=(note,))
            # One ID per attempt: PayPal answers a repeated ID with that attempt's answer.
            request_id = f"dmd-capture-{payment_id}-{refused}"
            try:
                self.client().post(f"{ORDERS}/{order_id}/capture", {}, request_id=request_id)
            except PayPalUnavailable as exc:
                if exc.issue in PAYER_AGAIN:
                    refused += 1
                    if exc.issue == "MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED":
                        refused = MAX_CAPTURE_ATTEMPTS  # PayPal will not take this order
                    wait = RETRY_AFTER_SECONDS * 2 ** (refused - 1)
                    self._refused[order_id] = (refused, self._clock() + wait)
                    log.warning(
                        "paypal refused a capture",
                        extra={
                            "event": {
                                "payment_intent_id": payment_id,
                                "attempt": refused,
                                "error": str(exc),
                            }
                        },
                    )
                    note = (
                        f"{order_id}: PayPal refused the payment ({exc.issue}); the payer can "
                        "open the payment page again and choose another way to pay"
                    )
                    return PaymentObservation(payment_id, self.name, now, notes=(note,))
                if exc.issue != "ORDER_ALREADY_CAPTURED":
                    raise AdapterUnavailable(str(exc), rate_limited=exc.rate_limited) from exc
            # Read back in full (the capture's answer leaves parts of the order out).
            order = self._order(order_id, expected)

        if order.get("status") != "COMPLETED":
            note = f"{order_id}: PayPal order {order.get('status') or 'unknown'}, not paid"
            return PaymentObservation(payment_id, self.name, now, notes=(note,))
        return self._completed(order, order_id, expected, now)

    def _order(self, order_id: str, expected: ExpectedPayment) -> dict[str, Any]:
        order = self._paypal(lambda paypal: paypal.get(f"{ORDERS}/{order_id}"))
        self._check_belongs(order, order_id, expected.payment_intent_id, expected.amount_base_units)
        return order

    def _completed(
        self, order: Mapping[str, Any], order_id: str, expected: ExpectedPayment, now: datetime
    ) -> PaymentObservation:
        payment_id = expected.payment_intent_id
        unit = order["purchase_units"][0]
        payments = unit.get("payments") if isinstance(unit.get("payments"), dict) else {}
        captures = payments.get("captures") if isinstance(payments, dict) else None
        if (
            not isinstance(captures, list)
            or len(captures) != 1
            or not isinstance(captures[0], dict)
        ):
            raise AdapterUnavailable(f"{order_id}: completed, but not with exactly one capture")
        capture = captures[0]
        capture_id = str(capture.get("id") or "")
        status = str(capture.get("status") or "")
        reason = ""
        if isinstance(capture.get("status_details"), dict):
            reason = str(capture["status_details"].get("reason") or "")
        amount = capture.get("amount") if isinstance(capture.get("amount"), dict) else {}
        cents = usd_cents(amount.get("value"))
        problems = []
        if not capture_id:
            problems.append("no capture ID")
        if capture.get("custom_id") not in (None, payment_id):
            problems.append("the capture is for another payment")
        if amount.get("currency_code") != CURRENCY:
            problems.append("not in US dollars")
        if cents != expected.amount_base_units:
            problems.append("not the quoted amount")
        if capture.get("final_capture") is False:
            problems.append("not the final capture")
        if problems:
            # Paid on PayPal's side but not as expected: nothing is credited, a person checks.
            raise AdapterUnavailable(
                f"{order_id}: unexpected PayPal payment ({', '.join(problems)})"
            )

        disputed = reason in DISPUTE_REASONS
        if status in ("DECLINED", "FAILED") or (status == "PENDING" and not disputed):
            note = f"{capture_id}: PayPal capture {status}" + (f" ({reason})" if reason else "")
            return PaymentObservation(payment_id, self.name, now, notes=(note,))
        if status not in ("COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED", "PENDING"):
            raise AdapterUnavailable(f"{capture_id}: unknown PayPal capture status {status!r}")

        notes = []
        if status == "PARTIALLY_REFUNDED":
            notes.append(f"{capture_id}: partly refunded on PayPal (partial refund)")
        if not disputed:
            disputed = self._disputed(capture_id)
        output = ObservedOutput(
            transaction_reference=capture_id,
            output_index=0,
            amount_base_units=int(cents or 0),
            confirmations=1,  # a PayPal payment is final when it completes
            first_seen_at=_time(capture.get("create_time")) or now,
        )
        return PaymentObservation(
            payment_intent_id=payment_id,
            adapter=self.name,
            observed_at=now,
            outputs=(output,),
            notes=tuple(notes),
            refunded=status == "REFUNDED",
            disputed=disputed,
        )

    def _disputed(self, capture_id: str) -> bool:
        """Whether the payer opened a dispute or chargeback on this payment (PayPal's
        disputes, when the app may read them; otherwise the capture's own status says it)."""
        paypal = self.client()
        if DISPUTES_SCOPE not in paypal.scopes:
            return False
        answer = self._paypal(
            lambda client: client.get(DISPUTES, {"disputed_transaction_id": capture_id})
        )
        items = answer.get("items")
        return isinstance(items, list) and len(items) > 0

    def _check_belongs(
        self, order: Mapping[str, Any], order_id: str, payment_intent_id: str, cents: int
    ) -> None:
        """A PayPal order counts only if it is this payment intent's, for its amount."""
        units = order.get("purchase_units")
        unit = units[0] if isinstance(units, list) and len(units) == 1 else None
        if not isinstance(unit, dict) or order.get("id") != order_id:
            raise AdapterUnavailable(
                f"PayPal order {order_id or '?'} is not for {payment_intent_id}"
            )
        if (
            unit.get("custom_id") != payment_intent_id
            or unit.get("reference_id") != payment_intent_id
            or order.get("intent") != "CAPTURE"
        ):
            raise AdapterUnavailable(f"PayPal order {order_id} is not for {payment_intent_id}")
        amount = unit.get("amount") if isinstance(unit.get("amount"), dict) else {}
        if amount.get("currency_code") != CURRENCY or usd_cents(amount.get("value")) != cents:
            raise AdapterUnavailable(f"PayPal order {order_id} is not for the quoted amount")


def _time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
