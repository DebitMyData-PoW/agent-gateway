"""Restricted JSON-RPC client for a DebitMyData Core node (SEC-05..SEC-07).

Only the read-only methods the payment adapter needs can be called; everything else is
refused before a request is sent, so no code path can unlock a wallet, export keys, send
funds, mine, or administer the node through the gateway.

Amounts are parsed as ``Decimal`` (never float), and credentials are never logged.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import Mapping
from decimal import Decimal
from itertools import count
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger(__name__)

ALLOWED_METHODS = frozenset(
    {
        "getblockchaininfo",
        "getwalletinfo",
        "getnewaddress",
        "getaddressinfo",
        "listreceivedbyaddress",
        "gettransaction",
        "getblockheader",
        "decoderawtransaction",
        # For the DMD mining-cost price (pricing/mining_cost.py).
        "getblockhash",
    }
)

# RPC error codes from DMD Core (inherited from Bitcoin Core).
RPC_INVALID_ADDRESS_OR_KEY = -5


class RpcError(RuntimeError):
    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method = method
        self.code = code


class RpcUnavailable(RuntimeError):
    """The node could not be reached or answered with an unexpected response."""


class MethodNotAllowed(RuntimeError):
    pass


def _require_private_endpoint(url: str) -> None:
    """Node RPC must stay on loopback or a private network (SEC-04)."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("DMD RPC URL must look like http://<private-ip>:<port>")
    if parsed.username or parsed.password:
        raise ValueError("put RPC credentials in DMD_RPC_USER / DMD_RPC_PASSWORD, not the URL")
    host = parsed.hostname
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("DMD RPC URL must use localhost or a private IP address") from exc
    if not (address.is_loopback or address.is_private):
        raise ValueError("DMD RPC URL must point to a loopback or private IP address")


class DmdRpcClient:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        wallet: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        _require_private_endpoint(url)
        self._endpoint = f"{url.rstrip('/')}/wallet/{quote(wallet, safe='')}"
        self._client = httpx.Client(
            auth=(user, password),
            timeout=timeout_seconds,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )
        self._ids = count(1)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> DmdRpcClient:
        missing = [
            name
            for name in ("DMD_RPC_URL", "DMD_RPC_USER", "DMD_RPC_PASSWORD", "DMD_RPC_WALLET")
            if not (environ.get(name) or "").strip()
        ]
        if missing:
            raise RpcUnavailable("missing DMD RPC settings: " + ", ".join(missing))
        return cls(
            environ["DMD_RPC_URL"].strip(),
            environ["DMD_RPC_USER"].strip(),
            environ["DMD_RPC_PASSWORD"],
            environ["DMD_RPC_WALLET"].strip(),
            timeout_seconds=float(environ.get("DMD_RPC_TIMEOUT_SECONDS") or 10),
        )

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        if method not in ALLOWED_METHODS:
            raise MethodNotAllowed(f"RPC method {method!r} is not allowed")
        payload = {
            "jsonrpc": "1.0",
            "id": next(self._ids),
            "method": method,
            "params": dict(params or {}),
        }
        try:
            response = self._client.post(self._endpoint, content=json.dumps(payload))
        except httpx.HTTPError as exc:
            raise RpcUnavailable(f"{method}: node unreachable ({type(exc).__name__})") from exc
        if response.status_code == 401:
            raise RpcUnavailable(f"{method}: RPC authentication failed")
        try:
            body = json.loads(response.content, parse_float=Decimal)
        except ValueError as exc:
            raise RpcUnavailable(
                f"{method}: invalid response (HTTP {response.status_code})"
            ) from exc
        error = body.get("error") if isinstance(body, dict) else None
        if error:
            raise RpcError(method, error.get("code"), str(error.get("message")))
        if not isinstance(body, dict) or "result" not in body:
            raise RpcUnavailable(f"{method}: unexpected response shape")
        return body["result"]

    def close(self) -> None:
        self._client.close()
