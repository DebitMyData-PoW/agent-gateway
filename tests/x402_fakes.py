"""Test doubles for x402: an in-memory EVM chain with one EIP-3009 token, and a facilitator.

The chain answers the JSON-RPC methods the gateway uses, from blocks, receipts and logs it
builds the way a real chain would (``Transfer`` and ``AuthorizationUsed`` events). The
facilitator really checks the payer's EIP-712 signature, the amount, the recipient and the
balance before settling, so the gateway is tested against a payment that a real facilitator
would accept.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

from dmd_gateway.adapters.evm_rpc import ALLOWED_METHODS, EvmRpcClient, MethodNotAllowed
from dmd_gateway.adapters.x402 import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    X402PaymentAdapter,
    address_topic,
)
from dmd_gateway.adapters.x402_facilitator import (
    FacilitatorClient,
    FacilitatorUnavailable,
    SettleResult,
    VerifyResult,
)
from dmd_gateway.domain.routes import RailConfig
from dmd_gateway.services.staging_tools import TRANSFER_WITH_AUTHORIZATION
from dmd_gateway.services.x402_payments import encode_header

TOKEN = "0x2222222222222222222222222222222222222222"
MERCHANT = "0x1111111111111111111111111111111111111111"
CHAIN_ID = 8453
NETWORK = f"eip155:{CHAIN_ID}"


def _hash(*parts: object) -> str:
    return "0x" + hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()


@dataclass
class FakeBaseChain:
    chain_id: int = CHAIN_ID
    token: str = TOKEN
    tip: int = 1_000
    fork: int = 0
    # Blocks mined right after each settlement (confirmations arrive at once).
    auto_confirm: int = 5
    balances: dict[str, int] = field(default_factory=dict)
    used: set[tuple[str, str]] = field(default_factory=set)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    # Transaction -> the height it was mined at, for receipts that survive a reorg.
    mined_at: dict[str, int] = field(default_factory=dict)

    def block_hash(self, number: int) -> str:
        return _hash("block", number, self.fork)

    def mine(self, blocks: int = 1) -> None:
        self.tip += blocks

    def reorg(self) -> None:
        """Every block gets a new hash: receipts now point at blocks no longer in the chain."""
        self.fork += 1

    def transfer_with_authorization(self, authorization: Mapping[str, str]) -> str:
        payer = authorization["from"].lower()
        nonce = authorization["nonce"].lower()
        value = int(authorization["value"])
        if (payer, nonce) in self.used:
            raise ValueError("authorization is used")
        if self.balances.get(payer, 0) < value:
            raise ValueError("transfer amount exceeds balance")
        self.used.add((payer, nonce))
        self.balances[payer] -= value
        to = authorization["to"].lower()
        self.balances[to] = self.balances.get(to, 0) + value
        self.mine()
        number = self.tip
        tx = _hash("tx", payer, nonce)
        self.mined_at[tx] = number
        self.receipts[tx] = {
            "transactionHash": tx,
            "status": "0x1",
            "blockNumber": hex(number),
            "blockHash": self.block_hash(number),
            "logs": [
                {
                    "address": self.token,
                    "topics": [AUTHORIZATION_USED_TOPIC, address_topic(payer), nonce],
                    "data": "0x",
                    "logIndex": "0x0",
                    "transactionHash": tx,
                    "blockNumber": hex(number),
                },
                {
                    "address": self.token,
                    "topics": [TRANSFER_TOPIC, address_topic(payer), address_topic(to)],
                    "data": "0x" + format(value, "064x"),
                    "logIndex": "0x1",
                    "transactionHash": tx,
                    "blockNumber": hex(number),
                },
            ],
        }
        self.mine(self.auto_confirm)
        return tx

    def remine(self, tx: str) -> None:
        """After a reorg, include the transaction again in the new chain."""
        receipt = self.receipts[tx]
        self.mine()
        receipt["blockNumber"] = hex(self.tip)
        receipt["blockHash"] = self.block_hash(self.tip)
        for log in receipt["logs"]:
            log["blockNumber"] = hex(self.tip)
        self.mine(self.auto_confirm)

    # ------------------------------------------------------------------ JSON-RPC

    def rpc(self, method: str, params: list[Any]) -> Any:
        self.calls.append(method)
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            return hex(self.tip)
        if method == "eth_getCode":
            return "0x6080" if params[0].lower() == self.token.lower() else "0x"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            if number > self.tip:
                return None
            # Two-second blocks, the newest one just made.
            timestamp = int(time.time()) - 2 * (self.tip - number)
            return {
                "number": hex(number),
                "hash": self.block_hash(number),
                "timestamp": hex(timestamp),
            }
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0].lower())
        if method == "eth_getLogs":
            query = params[0]
            low, high = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            found = []
            for receipt in self.receipts.values():
                for log in receipt["logs"]:
                    number = int(log["blockNumber"], 16)
                    if log["address"].lower() != query["address"].lower():
                        continue
                    if not low <= number <= high:
                        continue
                    topics = query["topics"]
                    if all(t is None or t == log["topics"][i] for i, t in enumerate(topics)):
                        found.append(log)
            return found
        raise AssertionError(f"unexpected RPC method {method}")


class FakeRpc(EvmRpcClient):
    def __init__(self, chain: FakeBaseChain) -> None:  # no network
        self.chain = chain

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in ALLOWED_METHODS:
            raise MethodNotAllowed(method)
        return self.chain.rpc(method, params)


def recover_payer(payload: Mapping[str, Any], requirements: Mapping[str, Any]) -> str:
    authorization = payload["payload"]["authorization"]
    message = encode_typed_data(
        {
            "name": requirements["extra"]["name"],
            "version": requirements["extra"]["version"],
            "chainId": int(requirements["network"].split(":")[1]),
            "verifyingContract": requirements["asset"],
        },
        TRANSFER_WITH_AUTHORIZATION,
        {
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": bytes.fromhex(authorization["nonce"][2:]),
        },
    )
    return str(Account.recover_message(message, signature=payload["payload"]["signature"]))


class FakeFacilitator(FacilitatorClient):
    """Verifies like a facilitator would, and settles on the fake chain."""

    def __init__(self, chain: FakeBaseChain) -> None:  # no network
        self.chain = chain
        self.reset()

    def reset(self) -> None:
        self.verify_calls: list[dict[str, Any]] = []
        self.settle_calls: list[dict[str, Any]] = []
        self.refuse_verify: str | None = None
        self.settle_mode = "ok"  # ok | fail | down | down_after_broadcast | no_hash
        self.network_down = False

    @property
    def network(self) -> str:
        return f"eip155:{self.chain.chain_id}"

    def supported(self) -> dict[str, Any]:
        if self.network_down:
            raise FacilitatorUnavailable("down")
        return {"kinds": [{"x402Version": 2, "scheme": "exact", "network": self.network}]}

    def verify(self, payload: dict[str, Any], requirements: dict[str, Any]) -> VerifyResult:
        if self.network_down:
            raise FacilitatorUnavailable("down")
        self.verify_calls.append(payload)
        authorization = payload["payload"]["authorization"]
        payer = authorization["from"]
        if self.refuse_verify:
            return VerifyResult(False, self.refuse_verify, payer)
        if recover_payer(payload, requirements).lower() != payer.lower():
            return VerifyResult(False, "invalid_exact_evm_payload_signature", payer)
        if authorization["to"].lower() != requirements["payTo"].lower():
            return VerifyResult(False, "invalid_exact_evm_payload_recipient_mismatch", payer)
        if int(authorization["value"]) != int(requirements["amount"]):
            return VerifyResult(
                False, "invalid_exact_evm_payload_authorization_value_mismatch", payer
            )
        if self.chain.balances.get(payer.lower(), 0) < int(authorization["value"]):
            return VerifyResult(False, "insufficient_funds", payer)
        return VerifyResult(True, None, payer)

    def settle(self, payload: dict[str, Any], requirements: dict[str, Any]) -> SettleResult:
        self.settle_calls.append(payload)
        authorization = payload["payload"]["authorization"]
        payer = authorization["from"]
        if self.settle_mode == "down":
            raise FacilitatorUnavailable("timeout")
        if self.settle_mode == "fail":
            return SettleResult(False, "", self.network, payer, "insufficient_funds")
        tx = self.chain.transfer_with_authorization(authorization)
        if self.settle_mode == "down_after_broadcast":
            raise FacilitatorUnavailable("timeout after broadcast")
        if self.settle_mode == "no_hash":
            return SettleResult(True, "", self.network, payer, None)
        return SettleResult(True, tx, self.network, payer, None)


CHAIN = FakeBaseChain()
FACILITATOR = FakeFacilitator(CHAIN)


class TestX402Adapter(X402PaymentAdapter):
    """The real adapter, wired to the fake chain and facilitator."""

    __test__ = False  # not a pytest test class

    def __init__(self, rail_id: str, rail: RailConfig, environ: Mapping[str, str]) -> None:
        super().__init__(rail_id, rail, environ, rpc=FakeRpc(CHAIN), facilitator=FACILITATOR)


def reset_chain() -> None:
    CHAIN.__init__()  # type: ignore[misc]
    FACILITATOR.reset()


class TestPayer:
    """An agent's EVM wallet: signs x402 'exact' payments (EIP-3009)."""

    __test__ = False

    def __init__(self, balance: int = 100_000_000) -> None:
        self.account = Account.create()
        CHAIN.balances[self.address.lower()] = balance

    @property
    def address(self) -> str:
        return str(self.account.address)

    def payload(
        self,
        required: Mapping[str, Any],
        *,
        nonce: str | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        requirements = dict(required["accepts"][0])
        authorization = {
            "from": self.address,
            "to": requirements["payTo"],
            "value": requirements["amount"],
            "validAfter": "0",
            "validBefore": str(int(time.time()) + requirements["maxTimeoutSeconds"]),
            "nonce": nonce or "0x" + secrets.token_bytes(32).hex(),
        }
        authorization.update({k: str(v) for k, v in changes.items()})
        signed = Account.sign_typed_data(
            self.account.key,
            {
                "name": requirements["extra"]["name"],
                "version": requirements["extra"]["version"],
                "chainId": int(requirements["network"].split(":")[1]),
                "verifyingContract": requirements["asset"],
            },
            TRANSFER_WITH_AUTHORIZATION,
            {
                "from": authorization["from"],
                "to": authorization["to"],
                "value": int(authorization["value"]),
                "validAfter": int(authorization["validAfter"]),
                "validBefore": int(authorization["validBefore"]),
                "nonce": bytes.fromhex(authorization["nonce"][2:]),
            },
        )
        signature = signed.signature.hex()
        return {
            "x402Version": 2,
            "resource": required["resource"],
            "accepted": requirements,
            "payload": {
                "signature": signature if signature.startswith("0x") else "0x" + signature,
                "authorization": authorization,
            },
            "extensions": required.get("extensions", {}),
        }

    def header(self, required: Mapping[str, Any], **kwargs: Any) -> str:
        return encode_header(self.payload(required, **kwargs))
