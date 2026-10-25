"""Staging-only test tools: stand-ins for an agent's own key and wallet.

They let anyone try the whole DID registration from the API docs page of the STAGING server,
where every chain is a test chain whose coins have no value:

* a throwaway agent key and wallet (in production an agent creates its own and never shares
  the private keys);
* a signature over a controller challenge with that key or wallet (in production the agent
  signs);
* a payment of a DMD quote from the staging test wallet, followed by the blocks that confirm
  it (in production the agent pays from its own wallet and the chain confirms it);
* for an x402 quote, a payment signed by the staging test wallet on Base Sepolia (in
  production the agent's own wallet signs it). Signing moves nothing: the funds only move
  when the signed payment is sent to POST /v1/dids/registrations.

Three independent locks keep them out of production: settings refuse to enable them in the
production environment, their routes are only mounted when enabled, and the test payers
refuse anything but a test chain (the DMD payer checks that its node reports regtest; the
x402 payer only signs for Base Sepolia, and its signatures are bound to that chain). The DMD
payer has its own short list of RPC methods; the gateway's payment client stays read-only.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

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
_PRIVATE_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")  # an EVM private key


class StagingToolError(RuntimeError):
    """A test tool cannot do what was asked; the message says why."""


# --------------------------------------------------------------------------- test agent keys


@dataclass(frozen=True, slots=True)
class ThrowawayAgentKey:
    controller_public_key: str  # ed25519:z6Mk...
    private_key: str  # Multikey secret key, z3u2...
    wallet_address: str  # EVM address, 0x... (checksummed)
    wallet_private_key: str  # 0x + 64 hexadecimal characters


def new_test_agent_key() -> ThrowawayAgentKey:
    key = Ed25519PrivateKey.generate()
    wallet = Account.create()
    return ThrowawayAgentKey(
        _public(key), _encode_private(key), str(wallet.address), "0x" + bytes(wallet.key).hex()
    )


def is_wallet_key(text: str) -> bool:
    """A wallet private key (0x + 64 hex) rather than an Ed25519 one (z3u2...)."""
    return bool(_PRIVATE_KEY.match(text.strip()))


def load_test_wallet(text: str) -> LocalAccount:
    if not is_wallet_key(text):
        raise StagingToolError("wallet_private_key must be the 0x... value from test-agent-keys")
    value = text.strip()
    return Account.from_key(value if value.startswith("0x") else "0x" + value)


def sign_with_test_wallet(wallet: LocalAccount, message: str) -> str:
    """The EIP-191 (personal_sign) signature a wallet returns for a text message."""
    signature = wallet.sign_message(encode_defunct(text=message)).signature.hex()
    return signature if signature.startswith("0x") else "0x" + signature


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


# --------------------------------------------------------------------------- x402 test payer

BASE_SEPOLIA = "eip155:84532"
BASE_SEPOLIA_CHAIN_ID = 84532
STAGING_BASE_PAYER_KEY = "DMD_STAGING_BASE_PAYER_KEY"

# EIP-3009 TransferWithAuthorization, the message an x402 "exact" EVM payment signs.
TRANSFER_WITH_AUTHORIZATION = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


@dataclass(frozen=True, slots=True)
class SignedTestPayment:
    payer: str
    payment_payload: dict[str, Any]
    valid_before: int


class BaseSepoliaTestPayer:
    """Signs x402 payments with the staging test wallet, on Base Sepolia only.

    The key is a test wallet's key (``DMD_STAGING_BASE_PAYER_KEY``), funded with test USDC
    from a faucet. It never signs for another network, and an EIP-712 signature is bound to
    the chain and token contract it names, so it cannot be replayed on Base mainnet.
    """

    def __init__(self, private_key: str) -> None:
        if not _PRIVATE_KEY.match(private_key.strip()):
            raise StagingToolError(f"{STAGING_BASE_PAYER_KEY} must be 64 hexadecimal characters")
        self._account = Account.from_key(private_key.strip())

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> BaseSepoliaTestPayer:
        value = (environ.get(STAGING_BASE_PAYER_KEY) or "").strip()
        if not value:
            raise StagingToolError(f"{STAGING_BASE_PAYER_KEY} is not set on this server")
        return cls(value)

    @property
    def address(self) -> str:
        return str(self._account.address)

    def sign(
        self,
        requirements: Mapping[str, Any],
        resource: Mapping[str, Any] | None,
        extensions: Mapping[str, Any] | None,
        now: int,
    ) -> SignedTestPayment:
        if requirements.get("network") != BASE_SEPOLIA or requirements.get("scheme") != "exact":
            raise StagingToolError(
                "the x402 test payer only signs 'exact' payments on Base Sepolia"
            )
        extra = requirements.get("extra") or {}
        valid_before = now + int(requirements["maxTimeoutSeconds"])
        nonce = "0x" + secrets.token_bytes(32).hex()
        authorization = {
            "from": self.address,
            "to": str(requirements["payTo"]),
            "value": str(requirements["amount"]),
            "validAfter": "0",
            "validBefore": str(valid_before),
            "nonce": nonce,
        }
        signed = Account.sign_typed_data(
            self._account.key,
            {
                "name": str(extra.get("name")),
                "version": str(extra.get("version")),
                "chainId": BASE_SEPOLIA_CHAIN_ID,
                "verifyingContract": str(requirements["asset"]),
            },
            TRANSFER_WITH_AUTHORIZATION,
            {
                "from": self.address,
                "to": authorization["to"],
                "value": int(authorization["value"]),
                "validAfter": 0,
                "validBefore": valid_before,
                "nonce": bytes.fromhex(nonce[2:]),
            },
        )
        signature = signed.signature.hex()
        payload: dict[str, Any] = {
            "x402Version": 2,
            "accepted": dict(requirements),
            "payload": {
                "signature": signature if signature.startswith("0x") else "0x" + signature,
                "authorization": authorization,
            },
        }
        if resource:
            payload["resource"] = dict(resource)
        if extensions:
            payload["extensions"] = dict(extensions)  # echoed, as x402 clients do
        return SignedTestPayment(self.address, payload, valid_before)
