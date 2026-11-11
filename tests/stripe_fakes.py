"""A stand-in for the parts of Stripe's API the card rail uses (no network)."""

from __future__ import annotations

import itertools
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl

import httpx

from dmd_gateway.adapters.stripe_api import API_VERSION, StripeClient
from dmd_gateway.adapters.stripe_checkout import StripeCheckoutAdapter
from dmd_gateway.domain.routes import RailConfig

TEST_KEY = "rk_test_" + "k" * 24
_IDS = itertools.count(1)


class FakeStripe:
    """Checkout Sessions, PaymentIntents and Charges, kept in memory."""

    def __init__(self, *, livemode: bool = False) -> None:
        self.livemode = livemode
        self.sessions: dict[str, dict[str, Any]] = {}
        self.intents: dict[str, dict[str, Any]] = {}
        self.charges: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.created: list[dict[str, str]] = []  # the form fields of each session created
        self.by_idempotency_key: dict[str, str] = {}
        self.refuse: tuple[int, dict[str, Any]] | None = None  # answer every request with this
        self._ids = _IDS  # Stripe's IDs never repeat, even across fresh fakes

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ HTTP

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
        assert request.headers["Stripe-Version"] == API_VERSION
        if self.refuse is not None:
            status, body = self.refuse
            return httpx.Response(status, json=body)
        path = request.url.path
        if request.method == "GET" and path in (
            "/v1/checkout/sessions",
            "/v1/payment_intents",
            "/v1/charges",
        ):
            assert request.url.params.get("limit") == "1"
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})
        if request.method == "POST" and path == "/v1/checkout/sessions":
            fields = dict(parse_qsl(request.content.decode()))
            return httpx.Response(200, json=self._create(fields, request))
        if request.method == "GET" and path.startswith("/v1/checkout/sessions/"):
            session = self.sessions.get(path.rsplit("/", 1)[-1])
            if session is None:
                return httpx.Response(404, json={"error": {"type": "invalid_request_error"}})
            expand = request.url.params.get_list("expand[0]")
            return httpx.Response(200, json=self._expanded(session, expand))
        return httpx.Response(404, json={"error": {"type": "invalid_request_error"}})

    def _create(self, fields: dict[str, str], request: httpx.Request) -> dict[str, Any]:
        key = request.headers.get("Idempotency-Key")
        if key and key in self.by_idempotency_key:
            return self.sessions[self.by_idempotency_key[key]]
        self.created.append(fields)
        number = next(self._ids)
        session_id = f"cs_{'live' if self.livemode else 'test'}_{number:04d}"
        metadata = {
            name.removeprefix("metadata[").removesuffix("]"): value
            for name, value in fields.items()
            if name.startswith("metadata[")
        }
        session = {
            "id": session_id,
            "object": "checkout.session",
            "url": f"https://checkout.stripe.com/c/pay/{session_id}#fid",
            "status": "open",
            "payment_status": "unpaid",
            "mode": fields["mode"],
            "amount_total": int(fields["line_items[0][price_data][unit_amount]"])
            * int(fields["line_items[0][quantity]"]),
            "currency": fields["line_items[0][price_data][currency]"],
            "livemode": self.livemode,
            "client_reference_id": fields.get("client_reference_id"),
            "metadata": metadata,
            "expires_at": int(fields["expires_at"]),
            "payment_intent": None,
            "_pi_metadata": {
                name.removeprefix("payment_intent_data[metadata][").removesuffix("]"): value
                for name, value in fields.items()
                if name.startswith("payment_intent_data[metadata][")
            },
        }
        self.sessions[session_id] = session
        if key:
            self.by_idempotency_key[key] = session_id
        return self._public(session)

    @staticmethod
    def _public(item: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _expanded(self, session: dict[str, Any], expand: list[str]) -> dict[str, Any]:
        body = self._public(session)
        intent_id = session["payment_intent"]
        if intent_id and "payment_intent.latest_charge" in expand:
            intent = dict(self.intents[intent_id])
            intent["latest_charge"] = dict(self.charges[intent["latest_charge"]])
            body["payment_intent"] = intent
        return body

    # ------------------------------------------------------------------ what the payer does

    def pay(self, session_id: str, *, created: int | None = None, amount: int | None = None) -> str:
        """The payer pays the session's page with a card that works."""
        session = self.sessions[session_id]
        number = next(self._ids)
        intent_id, charge_id = f"pi_test_{number:04d}", f"ch_test_{number:04d}"
        amount = session["amount_total"] if amount is None else amount
        self.charges[charge_id] = {
            "id": charge_id,
            "object": "charge",
            "amount": amount,
            "amount_captured": amount,
            "amount_refunded": 0,
            "refunded": False,
            "disputed": False,
            "captured": True,
            "paid": True,
            "status": "succeeded",
            "currency": session["currency"],
            "livemode": self.livemode,
            "created": created if created is not None else session["expires_at"] - 1800,
            "payment_intent": intent_id,
        }
        self.intents[intent_id] = {
            "id": intent_id,
            "object": "payment_intent",
            "status": "succeeded",
            "amount": amount,
            "amount_received": amount,
            "currency": session["currency"],
            "livemode": self.livemode,
            "metadata": dict(session["_pi_metadata"]),
            "latest_charge": charge_id,
        }
        session.update(status="complete", payment_status="paid", payment_intent=intent_id, url=None)
        return intent_id

    def refund(self, session_id: str, cents: int | None = None) -> None:
        charge = self._charge(session_id)
        refunded = charge["amount"] if cents is None else cents
        charge.update(amount_refunded=refunded, refunded=refunded >= charge["amount"])

    def dispute(self, session_id: str) -> None:
        self._charge(session_id)["disputed"] = True

    def expire(self, session_id: str) -> None:
        self.sessions[session_id].update(status="expired", url=None)

    def _charge(self, session_id: str) -> dict[str, Any]:
        intent = self.intents[self.sessions[session_id]["payment_intent"]]
        return self.charges[intent["latest_charge"]]


STRIPE = FakeStripe()


class FakeStripeAdapter(StripeCheckoutAdapter):
    """The real card adapter, talking to ``STRIPE`` instead of Stripe."""

    def __init__(self, rail_id: str, rail: RailConfig, environ: Mapping[str, str]) -> None:
        super().__init__(
            rail_id, rail, environ, client=StripeClient(TEST_KEY, transport=STRIPE.transport)
        )


def form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


def body(response: httpx.Response) -> Any:
    return json.loads(response.content)
