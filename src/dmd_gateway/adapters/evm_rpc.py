"""Read-only JSON-RPC client for an EVM chain (Base), used to check x402 settlements.

The gateway never signs or sends EVM transactions: the x402 facilitator does that. This
client only reads the chain so that every settlement the facilitator reports is checked
independently before anything is credited. Methods outside ``ALLOWED_METHODS`` are refused
before a request is sent.

RPC provider addresses often contain an API key, so the URL is never logged or put in error
messages.
"""

from __future__ import annotations

import ipaddress
import json
from itertools import count
from typing import Any
from urllib.parse import urlsplit

import httpx

ALLOWED_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getTransactionReceipt",
        "eth_getLogs",
        "eth_getCode",
    }
)


class EvmRpcError(RuntimeError):
    """The node answered with a JSON-RPC error."""

    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message[:200]}")
        self.method = method
        self.code = code


class EvmRpcUnavailable(RuntimeError):
    """The node could not be reached or answered something unusable."""


class MethodNotAllowed(RuntimeError):
    pass


def require_https_or_loopback(url: str, what: str) -> str:
    """Remote services are reached over HTTPS; plain HTTP only on this machine (tests)."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{what} must be an http(s) URL")
    if parsed.scheme == "http":
        host = parsed.hostname
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ValueError(f"{what} must use https (plain http is only allowed on localhost)")
    return url.strip().rstrip("/")


class EvmRpcClient:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = require_https_or_loopback(url, "the chain RPC URL")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )
        self._ids = count(1)

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in ALLOWED_METHODS:
            raise MethodNotAllowed(f"RPC method {method!r} is not allowed")
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            response = self._client.post(self._url, content=json.dumps(payload))
        except httpx.HTTPError as exc:
            raise EvmRpcUnavailable(
                f"{method}: chain RPC unreachable ({type(exc).__name__})"
            ) from exc
        if response.status_code in (401, 403):
            raise EvmRpcUnavailable(f"{method}: chain RPC refused the request (check its URL/key)")
        if response.status_code == 429:
            raise EvmRpcUnavailable(f"{method}: chain RPC rate limit reached")
        try:
            body = response.json()
        except ValueError as exc:
            raise EvmRpcUnavailable(
                f"{method}: invalid response (HTTP {response.status_code})"
            ) from exc
        error = body.get("error") if isinstance(body, dict) else None
        if error:
            raise EvmRpcError(method, error.get("code"), str(error.get("message")))
        if not isinstance(body, dict) or "result" not in body:
            raise EvmRpcUnavailable(f"{method}: unexpected response shape")
        return body["result"]

    # ------------------------------------------------------------------ typed helpers

    def chain_id(self) -> int:
        return _quantity(self.call("eth_chainId", []), "eth_chainId")

    def block_number(self) -> int:
        return _quantity(self.call("eth_blockNumber", []), "eth_blockNumber")

    def block(self, number: int) -> dict[str, Any] | None:
        result = self.call("eth_getBlockByNumber", [hex(number), False])
        return result if isinstance(result, dict) else None

    def receipt(self, transaction_hash: str) -> dict[str, Any] | None:
        result = self.call("eth_getTransactionReceipt", [transaction_hash])
        return result if isinstance(result, dict) else None

    def logs(
        self, address: str, topics: list[str | None], from_block: int, to_block: int
    ) -> list[dict[str, Any]]:
        result = self.call(
            "eth_getLogs",
            [
                {
                    "address": address,
                    "topics": topics,
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                }
            ],
        )
        if not isinstance(result, list):
            raise EvmRpcUnavailable("eth_getLogs: unexpected response shape")
        return result

    def code(self, address: str) -> str:
        result = self.call("eth_getCode", [address, "latest"])
        if not isinstance(result, str):
            raise EvmRpcUnavailable("eth_getCode: unexpected response shape")
        return result

    def close(self) -> None:
        self._client.close()


def _quantity(value: Any, what: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EvmRpcUnavailable(f"{what}: unexpected value")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise EvmRpcUnavailable(f"{what}: unexpected value") from exc


def quantity(value: Any, what: str = "value") -> int:
    """Parse an EVM JSON-RPC quantity ("0x..." hex string)."""
    return _quantity(value, what)
