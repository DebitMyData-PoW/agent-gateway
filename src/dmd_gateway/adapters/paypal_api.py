"""A small client for the parts of PayPal's REST API the PayPal rail uses (Orders v2, and
reading disputes).

It signs in with the PayPal app's client ID and secret (OAuth 2.0 client credentials) and
keeps the access token until shortly before it ends. Requests and answers are JSON. The
secret and the access token go only into the Authorization header: they are never logged
and never part of an error message.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from typing import Any

import httpx

LIVE_API = "https://api-m.paypal.com"
SANDBOX_API = "https://api-m.sandbox.paypal.com"
TOKEN_PATH = "/v1/oauth2/token"  # noqa: S105 (an address, not a secret)
# An access token is used until this many seconds before PayPal says it ends.
TOKEN_MARGIN_SECONDS = 120

Params = Mapping[str, Any]


class PayPalUnavailable(RuntimeError):
    """No usable answer from PayPal (network, outage, credentials refused, too many requests).

    ``status``: PayPal's HTTP status, if it answered; ``issue``: the first issue PayPal named
    (``INSTRUMENT_DECLINED``, ``ORDER_ALREADY_CAPTURED``...), for the answers the adapter
    handles itself."""

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        issue: str = "",
        rate_limited: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.issue = issue
        self.rate_limited = rate_limited  # HTTP 429: too many requests, usually passes


# Bearer tokens and long secrets, if PayPal ever quoted one: left out of messages.
_SECRET = re.compile(r"\b(?:A21AA|Bearer\s+)[A-Za-z0-9._\-]+|\b[A-Za-z0-9_\-]{40,}\b")


def _short(text: Any, limit: int = 200) -> str:
    value = _SECRET.sub("[hidden]", " ".join(str(text).split()))
    return value if len(value) <= limit else value[: limit - 3] + "..."


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        secret: str,
        *,
        live: bool,
        base_url: str | None = None,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not client_id.strip() or not secret.strip():
            raise ValueError("the PayPal client ID or secret is empty")
        self.live = live
        self._credentials = (client_id.strip(), secret.strip())
        self._client = httpx.Client(
            base_url=base_url or (LIVE_API if live else SANDBOX_API),
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "DebitMyData-Agent-Gateway", "Accept": "application/json"},
        )
        self._lock = threading.Lock()
        self._token = ""
        self._token_until = 0.0
        self.scopes: frozenset[str] = frozenset()

    # ------------------------------------------------------------------ access token

    def access_token(self) -> str:
        """The app's access token, asked for again once it is about to end."""
        with self._lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            try:
                response = self._client.post(
                    TOKEN_PATH,
                    data={"grant_type": "client_credentials"},
                    auth=self._credentials,
                )
            except httpx.HTTPError as exc:
                raise PayPalUnavailable(f"PayPal is unreachable ({type(exc).__name__})") from exc
            body = _json(response)
            token = body.get("access_token") if isinstance(body, dict) else None
            if response.status_code != 200 or not isinstance(token, str) or not token:
                detail = ""
                if isinstance(body, dict) and body.get("error"):
                    detail = f": {_short(body.get('error'), 60)}"
                raise PayPalUnavailable(
                    f"PayPal refused the app's client ID and secret (HTTP "
                    f"{response.status_code}{detail})",
                    status=response.status_code,
                    rate_limited=response.status_code == 429,
                )
            lifetime = body.get("expires_in")
            seconds = lifetime if isinstance(lifetime, int) and lifetime > 0 else 300
            self._token = token
            self._token_until = time.monotonic() + max(30, seconds - TOKEN_MARGIN_SECONDS)
            self.scopes = frozenset(str(body.get("scope") or "").split())
            return token

    # ------------------------------------------------------------------ requests

    def get(self, path: str, params: Params | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=dict(params or {}))

    def post(
        self, path: str, body: Params | None = None, *, request_id: str | None = None
    ) -> dict[str, Any]:
        """A POST with a JSON body. ``request_id`` (PayPal-Request-Id): PayPal answers a
        repeated request with the same ID as it answered the first, without doing it again."""
        headers = {"Prefer": "return=representation", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        return self._request("POST", path, json=dict(body or {}), headers=headers)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {**(headers or {}), "Authorization": f"Bearer {self.access_token()}"}
        try:
            response = self._client.request(method, path, params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise PayPalUnavailable(f"PayPal is unreachable ({type(exc).__name__})") from exc
        body = _json(response)
        if response.status_code in (200, 201) and isinstance(body, dict):
            return body
        if response.status_code == 401:
            with self._lock:
                self._token = ""  # ended early: ask for a new one next time
        issue = ""
        detail = ""
        if isinstance(body, dict):
            details = body.get("details")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                issue = str(details[0].get("issue") or "")
            parts = [str(body.get(name)) for name in ("name",) if body.get(name)]
            if issue:
                parts.append(issue)
            detail = ": " + " / ".join(_short(part, 60) for part in parts) if parts else ""
            if body.get("debug_id"):
                detail += f" (debug_id {_short(body['debug_id'], 40)})"
        raise PayPalUnavailable(
            f"PayPal answered HTTP {response.status_code} to {method} {path}{detail}",
            status=response.status_code,
            issue=issue,
            rate_limited=response.status_code == 429,
        )


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
