"""Staging-only test tools: stand-ins for an agent's own key and DMD wallet.

They let anyone try the whole DID registration from the API docs page of the STAGING server,
where the DMD chain is a private regtest chain whose coins have no value:

* a throwaway agent key (in production an agent creates its key itself and never shares the
  private part);
* a signature over a controller challenge with that key (in production the agent signs);
* a payment of a quote from the staging test wallet, followed by the blocks that confirm it
  (in production the agent pays from its own wallet and the chain confirms it).

Three independent locks keep them out of production: settings refuse to enable them in the
production environment, their routes are only mounted when enabled, and the test payer
refuses to send anything unless the node reports the regtest chain. The payer has its own
short list of RPC methods; the gateway's payment client stays read-only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dmd_gateway.adapters.dmd_rpc import _require_private_endpoint
from dmd_gateway.identity.crypto import (
    ED25519_PUB_MULTICODEC,
    EncodingError,
    multibase_decode,
    multibase_encode,
)

# Multikey private key header for Ed25519 (multicodec ed25519-priv, 0x1300, as a varint):
# the encoded key starts with "z3u2".
ED25519_PRIV_MULTICODEC = b"\x80\x26"

PAYER_METHODS = frozenset(
    {"getblockchaininfo", "getbalance", "getnewaddress", "sendtoaddress", "generatetoaddress"}
)
# Coinbase coins mature after 250 blocks on DMD; mining a little more makes some spendable.
TOP_UP_BLOCKS = 260
FEE_MARGIN = Decimal("0.01")  # room for the transaction fee


class StagingToolError(RuntimeError):
    """A test tool cannot do what was asked; the message says why."""


# --------------------------------------------------------------------------- test agent keys


@dataclass(frozen=True, slots=True)
class ThrowawayAgentKey:
    controller_public_key: str  # ed25519:z6Mk...
    private_key: str  # Multikey secret key, z3u2...


def new_test_agent_key() -> ThrowawayAgentKey:
    key = Ed25519PrivateKey.generate()
    return ThrowawayAgentKey(_public(key), _encode_private(key))


def load_test_agent_key(text: str) -> Ed25519PrivateKey:
    try:
        raw = multibase_decode(text.strip())
    except EncodingError as exc:
        raise StagingToolError(
            "private_key must be the z3u2... value from test-agent-keys"
        ) from exc
    if len(raw) != 34 or not raw.startswith(ED25519_PRIV_MULTICODEC):
        raise StagingToolError("private_key must be the z3u2... value from test-agent-keys")
    return Ed25519PrivateKey.from_private_bytes(raw[2:])


def public_key_of(key: Ed25519PrivateKey) -> str:
    return _public(key)


def _public(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed25519:" + multibase_encode(ED25519_PUB_MULTICODEC + raw)


def _encode_private(key: Ed25519PrivateKey) -> str:
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return multibase_encode(ED25519_PRIV_MULTICODEC + seed)


# --------------------------------------------------------------------------- test payer


@dataclass(frozen=True, slots=True)
class StagingPayment:
    transaction_reference: str
    blocks_mined: int
    topped_up: bool


class RegtestPayer:
    """Pays quotes from the staging test wallet on a regtest node, then mines blocks."""

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        wallet: str,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            _require_private_endpoint(url)
        except ValueError as exc:
            raise StagingToolError(str(exc)) from exc
        self._endpoint = f"{url.rstrip('/')}/wallet/{quote(wallet, safe='')}"
        self._client = httpx.Client(
            auth=(user, password),
            timeout=timeout_seconds,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )
        self._ids = count(1)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str], wallet: str) -> RegtestPayer:
        missing = [
            name
            for name in ("DMD_RPC_URL", "DMD_RPC_USER", "DMD_RPC_PASSWORD")
            if not (environ.get(name) or "").strip()
        ]
        if missing:
            raise StagingToolError("missing DMD RPC settings: " + ", ".join(missing))
        return cls(
            environ["DMD_RPC_URL"].strip(),
            environ["DMD_RPC_USER"].strip(),
            environ["DMD_RPC_PASSWORD"],
            wallet,
        )

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        if method not in PAYER_METHODS:
            raise StagingToolError(f"RPC method {method!r} is not available to the test payer")
        payload = {
            "jsonrpc": "1.0",
            "id": next(self._ids),
            "method": method,
            "params": dict(params or {}),
        }
        try:
            response = self._client.post(self._endpoint, content=json.dumps(payload))
            body = json.loads(response.content, parse_float=Decimal)
        except (httpx.HTTPError, ValueError) as exc:
            raise StagingToolError(f"the test wallet's node did not answer {method}") from exc
        error = body.get("error") if isinstance(body, dict) else None
        if error:
            raise StagingToolError(f"{method} failed: {error.get('message')}")
        if not isinstance(body, dict) or "result" not in body:
            raise StagingToolError(f"{method}: unexpected answer from the node")
        return body["result"]

    def pay(self, address: str, amount: Decimal, confirmations: int) -> StagingPayment:
        chain = self.call("getblockchaininfo").get("chain")
        if chain != "regtest":
            raise StagingToolError(
                f"test payments only run on a regtest chain; this node is on {chain!r}"
            )
        miner = self.call("getnewaddress")
        topped_up = False
        if Decimal(self.call("getbalance")) < amount + FEE_MARGIN:
            self.call("generatetoaddress", {"nblocks": TOP_UP_BLOCKS, "address": miner})
            topped_up = True
            if Decimal(self.call("getbalance")) < amount + FEE_MARGIN:
                raise StagingToolError("the test wallet has no spendable test coins left")
        txid = self.call("sendtoaddress", {"address": address, "amount": str(amount)})
        self.call("generatetoaddress", {"nblocks": confirmations, "address": miner})
        return StagingPayment(str(txid), confirmations, topped_up)

    def close(self) -> None:
        self._client.close()
