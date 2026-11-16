"""A stand-in for the parts of PayPal's REST API the PayPal rail uses (no network)."""

from __future__ import annotations

import base64
import itertools
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from dmd_gateway.adapters.paypal_api import PayPalClient
from dmd_gateway.adapters.paypal_checkout import PayPalCheckoutAdapter
from dmd_gateway.domain.routes import RailConfig

CLIENT_ID = "AclientIDforTests" + "c" * 30
SECRET = "EsecretForTests" + "s" * 30
TOKEN = "A21AA" + "t" * 40
DISPUTES_SCOPE = "https://uri.paypal.com/services/disputes/read-seller"
_IDS = itertools.count(1)
JSON = {"Content-Type": "application/json"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakePayPal:
    """OAuth tokens, orders, captures and disputes, kept in memory."""

    def __init__(self, *, live: bool = False, scopes: str = DISPUTES_SCOPE) -> None:
        self.live = live
        self.scopes = scopes
        self.orders: dict[str, dict[str, Any]] = {}
        self.disputes: dict[str, list[dict[str, Any]]] = {}  # capture ID -> disputes
        self.requests: list[httpx.Request] = []
        self.created: list[dict[str, Any]] = []  # the body of each order created
        self.token_requests = 0
        self.by_request_id: dict[str, tuple[int, bytes]] = {}
        self.refuse: tuple[int, dict[str, Any]] | None = None  # answer every request with this
        self.decline_next_capture = False
        self.refuse_next_capture: str | None = None  # the issue PayPal answers with
        self.refuse_every_capture: str | None = None
        self.hold_next_capture: str | None = None  # a capture PENDING for this reason
        self._ids = _IDS

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ HTTP

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.refuse is not None:
            status, body = self.refuse
            return httpx.Response(status, json=body)
        path = request.url.path
        if request.method == "POST" and path == "/v1/oauth2/token":
            return self._token(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "invalid_token"})
        request_id = request.headers.get("PayPal-Request-Id")
        if request_id and request_id in self.by_request_id:
            status, content = self.by_request_id[request_id]  # the first answer, again
            return httpx.Response(status, content=content, headers=JSON)
        response = self._route(request, path)
        if request_id:
            self.by_request_id[request_id] = (response.status_code, response.read())
        return response

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        expected = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client"})
        assert request.content == b"grant_type=client_credentials"
        return httpx.Response(
            200,
            json={
                "access_token": TOKEN,
                "token_type": "Bearer",
                "expires_in": 32400,
                "scope": f"https://uri.paypal.com/services/payments/payment {self.scopes}".strip(),
            },
        )

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "POST" and path == "/v2/checkout/orders":
            return httpx.Response(201, json=self._create(json.loads(request.content)))
        if path.startswith("/v2/checkout/orders/"):
            order_id = path.removeprefix("/v2/checkout/orders/").removesuffix("/capture")
            order = self.orders.get(order_id)
            if order is None:
                return _error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
            if request.method == "GET":
                return httpx.Response(200, json=self._public(order))
            if request.method == "POST" and path.endswith("/capture"):
                return self._capture(order)
        if request.method == "GET" and path == "/v1/customer/disputes":
            capture_id = request.url.params.get("disputed_transaction_id", "")
            return httpx.Response(200, json={"items": self.disputes.get(capture_id, [])})
        return _error(404, "RESOURCE_NOT_FOUND", "NOT_FOUND")

    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created.append(body)
        number = next(self._ids)
        order_id = f"5O{number:06d}TESTORDER"
        host = "www.paypal.com" if self.live else "www.sandbox.paypal.com"
        unit = dict(body["purchase_units"][0])
        order = {
            "id": order_id,
            "intent": body["intent"],
            "status": "PAYER_ACTION_REQUIRED",
            "create_time": _now(),
            "purchase_units": [unit],
            "links": [
                {
                    "href": f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{order_id}",
                    "rel": "self",
                    "method": "GET",
                },  # noqa: E501
                {
                    "href": f"https://{host}/checkoutnow?token={order_id}",
                    "rel": "payer-action",
                    "method": "GET",
                },  # noqa: E501
            ],
        }
        self.orders[order_id] = order
        return self._public(order)

    def _capture(self, order: dict[str, Any]) -> httpx.Response:
        if order["status"] == "COMPLETED":
            return _error(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED")
        if order["status"] != "APPROVED":
            return _error(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED")
        if self.decline_next_capture:
            self.decline_next_capture = False
            return _error(422, "UNPROCESSABLE_ENTITY", "INSTRUMENT_DECLINED")
        if self.refuse_next_capture or self.refuse_every_capture:
            issue = self.refuse_next_capture or self.refuse_every_capture or ""
            self.refuse_next_capture = None
            return _error(
                422, "UNPROCESSABLE_ENTITY", issue, "The requested transaction was refused."
            )
        unit = order["purchase_units"][0]
        number = next(self._ids)
        capture: dict[str, Any] = {
            "id": f"9C{number:06d}TESTCAPTURE",
            "status": "COMPLETED",
            "amount": dict(unit["amount"]),
            "final_capture": True,
            "custom_id": unit.get("custom_id"),
            "invoice_id": unit.get("invoice_id"),
            "create_time": _now(),
            "update_time": _now(),
        }
        if self.hold_next_capture:
            capture.update(status="PENDING", status_details={"reason": self.hold_next_capture})
            self.hold_next_capture = None
        unit["payments"] = {"captures": [capture]}
        order.update(status="COMPLETED")
        # Like PayPal's capture answer: parts of the order only (no amount, no custom_id).
        short_unit = {"reference_id": unit["reference_id"], "payments": unit["payments"]}
        return httpx.Response(
            201, json={"id": order["id"], "status": "COMPLETED", "purchase_units": [short_unit]}
        )

    @staticmethod
    def _public(order: Mapping[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(order))

    # ------------------------------------------------------------------ what the payer does

    def approve(self, order_id: str) -> None:
        """The payer signs in on PayPal's page and approves the payment."""
        # PayPal's order shows no update_time: approving again looks the same.
        self.orders[order_id]["status"] = "APPROVED"

    def capture_of(self, order_id: str) -> dict[str, Any]:
        return self.orders[order_id]["purchase_units"][0]["payments"]["captures"][0]

    def complete_held(self, order_id: str) -> None:
        capture = self.capture_of(order_id)
        capture["status"] = "COMPLETED"
        capture.pop("status_details", None)

    def refund(self, order_id: str, *, partly: bool = False) -> None:
        self.capture_of(order_id)["status"] = "PARTIALLY_REFUNDED" if partly else "REFUNDED"

    def dispute(self, order_id: str) -> None:
        capture_id = self.capture_of(order_id)["id"]
        self.disputes[capture_id] = [{"dispute_id": "PP-D-1", "status": "OPEN"}]

    def capture_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/capture")]


def _error(status: int, name: str, issue: str, description: str = "") -> httpx.Response:
    detail = {"issue": issue, **({"description": description} if description else {})}
    return httpx.Response(
        status, json={"name": name, "details": [detail], "debug_id": "f0a1b2c3d4e5"}
    )


PAYPAL = FakePayPal()


class FakePayPalAdapter(PayPalCheckoutAdapter):
    """The real PayPal adapter, talking to ``PAYPAL`` instead of PayPal."""

    def __init__(self, rail_id: str, rail: RailConfig, environ: Mapping[str, str]) -> None:
        client = PayPalClient(CLIENT_ID, SECRET, live=False, transport=PAYPAL.transport)
        super().__init__(rail_id, rail, environ, client=client)
