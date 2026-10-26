"""Client for an x402 facilitator (x402 protocol version 2, section 7).

The facilitator checks a payer's signed authorization (``/verify``, read-only) and
broadcasts it (``/settle``), paying the network fee. The gateway trusts neither answer on
its own: what it credits comes from its own reading of the chain (see ``adapters/x402.py``).

Any facilitator that speaks the standard HTTP interface works: the public x402.org one on
test networks, or Coinbase's (CDP) on Base mainnet, which needs an API key (``auth``, see
``adapters/cdp_auth.py``) and is the one that lists paid endpoints in the x402 Bazaar.
Signatures and keys are never logged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from dmd_gateway.adapters.evm_rpc import require_https_or_loopback

X402_VERSION = 2


class FacilitatorUnavailable(RuntimeError):
    """No usable answer: for /settle this means the outcome is unknown."""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    is_valid: bool
    invalid_reason: str | None
    payer: str | None


@dataclass(frozen=True, slots=True)
class SettleResult:
    success: bool
    transaction: str
    network: str
    payer: str | None
    error_reason: str | None

    def as_payment_response(self) -> dict[str, Any]:
        """The ``SettlementResponse`` object for the PAYMENT-RESPONSE header."""
        body: dict[str, Any] = {
            "success": self.success,
            "transaction": self.transaction,
            "network": self.network,
        }
        if self.payer:
            body["payer"] = self.payer
        if self.error_reason:
            body["errorReason"] = self.error_reason
        return body


def _short(value: Any, limit: int = 120) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class FacilitatorClient:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
        auth: Callable[[str, str], dict[str, str]] | None = None,
    ) -> None:
        self._url = require_https_or_loopback(url, "the x402 facilitator URL").rstrip("/")
        self._auth = auth  # headers for one request: (method, url) -> headers
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    @property
    def authenticated(self) -> bool:
        return self._auth is not None

    def _auth_headers(self, method: str, url: str) -> dict[str, str]:
        if self._auth is None:
            return {}
        try:
            return self._auth(method, url)
        except ValueError as exc:
            raise FacilitatorUnavailable(f"facilitator credentials unusable: {exc}") from exc

    def _post(self, path: str, payload: dict[str, Any], requirements: dict[str, Any]) -> Any:
        body = {
            "x402Version": X402_VERSION,
            "paymentPayload": payload,
            "paymentRequirements": requirements,
        }
        url = f"{self._url}{path}"
        try:
            response = self._client.post(url, json=body, headers=self._auth_headers("POST", url))
        except httpx.HTTPError as exc:
            raise FacilitatorUnavailable(
                f"facilitator {path} unreachable ({type(exc).__name__})"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise FacilitatorUnavailable(
                f"facilitator {path}: invalid response (HTTP {response.status_code})"
            ) from exc
        # A refusal may come with HTTP 200 or a 4xx status; both carry the standard fields.
        if response.status_code >= 500 or not isinstance(data, dict):
            raise FacilitatorUnavailable(f"facilitator {path}: HTTP {response.status_code}")
        return data

    def supported(self) -> dict[str, Any]:
        url = f"{self._url}/supported"
        try:
            response = self._client.get(url, headers=self._auth_headers("GET", url))
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FacilitatorUnavailable("facilitator /supported unreachable") from exc
        if response.status_code != 200 or not isinstance(data, dict):
            raise FacilitatorUnavailable(f"facilitator /supported: HTTP {response.status_code}")
        return data

    def supports(self, scheme: str, network: str) -> bool:
        kinds = self.supported().get("kinds") or []
        return any(
            isinstance(kind, dict)
            and kind.get("x402Version") == X402_VERSION
            and kind.get("scheme") == scheme
            and kind.get("network") == network
            for kind in kinds
        )

    def verify(self, payload: dict[str, Any], requirements: dict[str, Any]) -> VerifyResult:
        data = self._post("/verify", payload, requirements)
        if not isinstance(data.get("isValid"), bool):
            raise FacilitatorUnavailable("facilitator /verify: answer without isValid")
        return VerifyResult(
            is_valid=data["isValid"],
            invalid_reason=_short(data.get("invalidReason") or data.get("invalidMessage")),
            payer=_short(data.get("payer"), 64),
        )

    def settle(self, payload: dict[str, Any], requirements: dict[str, Any]) -> SettleResult:
        data = self._post("/settle", payload, requirements)
        if not isinstance(data.get("success"), bool):
            raise FacilitatorUnavailable("facilitator /settle: answer without success")
        transaction = data.get("transaction")
        return SettleResult(
            success=data["success"],
            transaction=transaction if isinstance(transaction, str) else "",
            network=str(data.get("network") or requirements.get("network") or ""),
            payer=_short(data.get("payer"), 64),
            error_reason=_short(data.get("errorReason") or data.get("errorMessage")),
        )

    def close(self) -> None:
        self._client.close()
