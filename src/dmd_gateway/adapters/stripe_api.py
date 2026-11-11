"""A small client for the parts of Stripe's API the card rail uses (Checkout Sessions).

Requests are form-encoded, as Stripe's API expects, and carry a pinned API version so that
Stripe's answers keep the shape this code reads. The key goes only into the Authorization
header: it is never logged and never part of an error message.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

import httpx

API_BASE = "https://api.stripe.com"
# Stripe API version the requests ask for (the version of the API reference this code was
# written against). Stripe keeps old versions working; a newer one is adopted on purpose.
API_VERSION = "2026-10-28.endive"

Params = Mapping[str, Any]


class StripeUnavailable(RuntimeError):
    """No usable answer from Stripe (network, outage, key refused, too many requests)."""

    def __init__(self, message: str = "", *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited  # HTTP 429: too many requests, usually passes


def form_fields(params: Params, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding of nested values: ``a[b][0][c]=...``."""
    fields: list[tuple[str, str]] = []
    for name, value in params.items():
        key = f"{prefix}[{name}]" if prefix else str(name)
        fields.extend(_encode(key, value))
    return fields


def _encode(key: str, value: Any) -> list[tuple[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return form_fields(value, key)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        fields: list[tuple[str, str]] = []
        for index, item in enumerate(value):
            fields.extend(_encode(f"{key}[{index}]", item))
        return fields
    if isinstance(value, bool):
        return [(key, "true" if value else "false")]
    return [(key, str(value))]


# Stripe's own messages can quote a masked key ("rk_test_****1234"): left out all the same.
_KEY = re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9*]+|\bwhsec_[A-Za-z0-9*]+")


def _short(text: Any, limit: int = 200) -> str:
    value = _KEY.sub("[key]", " ".join(str(text).split()))
    return value if len(value) <= limit else value[: limit - 3] + "..."


class StripeClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = API_BASE,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("the Stripe API key is empty")
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Stripe-Version": API_VERSION,
                "User-Agent": "DebitMyData-Agent-Gateway",
            },
        )

    def get(self, path: str, params: Params | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=form_fields(params or {}))

    def post(
        self, path: str, data: Params | None = None, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", path, data=form_fields(data or {}), headers=headers)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        data: list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        content = None
        headers = dict(headers or {})
        if data is not None:
            content = urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = self._client.request(
                method, path, params=params, content=content, headers=headers
            )
        except httpx.HTTPError as exc:
            raise StripeUnavailable(f"Stripe is unreachable ({type(exc).__name__})") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 200 and isinstance(body, dict):
            return body
        error = body.get("error") if isinstance(body, dict) else None
        detail = ""
        if isinstance(error, dict):
            parts = [str(error.get(name)) for name in ("type", "code") if error.get(name)]
            message = error.get("message")
            detail = ": " + " / ".join(parts) if parts else ""
            if message:
                detail += f" ({_short(message)})"
        raise StripeUnavailable(
            f"Stripe answered HTTP {response.status_code} to {method} {path}{detail}",
            rate_limited=response.status_code == 429,
        )
