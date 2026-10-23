"""x402 payment adapter: USDC through x402 on Base (X402-01..X402-11).

How a payment works:
  * The quote names the exact amount, the token contract, the network and DebitMyData's
    receiving address (one merchant address from the environment).
  * The agent answers the HTTP 402 of ``POST /v1/dids/registrations`` with a signed EIP-3009
    ``transferWithAuthorization`` (the x402 "exact" scheme). The gateway checks it against
    the quote, has the facilitator verify it, then has the facilitator broadcast it. The
    gateway itself never holds keys and never sends a transaction.
  * The adapter then checks the settlement on chain itself (directive: "independently
    confirms the correct asset, network, destination, amount ... and settlement/finality
    status"): the transaction must have succeeded, must contain the token contract's
    ``AuthorizationUsed`` event for exactly the payer and nonce the gateway received for
    this intent, and the ``Transfer`` from that payer to the intent's receiving address.
    Its block must still be in the chain, and confirmations are counted from the chain tip.

Because the receiving address is shared, a transfer counts for an intent only through the
authorization received for that intent; transaction references sent by payers are never
used on this rail. Each transfer is credited once (``credited_outputs``, keyed by
transaction and log index).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar

from dmd_gateway.adapters.base import (
    AdapterUnavailable,
    ExpectedPayment,
    ObservedOutput,
    PaymentAdapter,
    PaymentDestination,
    PaymentObservation,
    SubmittedAuthorization,
)
from dmd_gateway.adapters.evm_rpc import (
    EvmRpcClient,
    EvmRpcError,
    EvmRpcUnavailable,
    quantity,
)
from dmd_gateway.adapters.x402_facilitator import FacilitatorClient, FacilitatorUnavailable
from dmd_gateway.domain.routes import EVM_ADDRESS, RailConfig

# keccak256 of the event signatures (checked in the tests).
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
AUTHORIZATION_USED_TOPIC = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"

# One eth_getLogs request covers at most this many blocks. An authorization is valid for a
# few minutes at most (``maxTimeoutSeconds``), so its use is always within this range of the
# height recorded when it was submitted (about 2.8 hours of 2-second Base blocks).
LOG_SEARCH_BLOCKS = 5_000
MAX_TIMEOUT_SECONDS = 300

T = TypeVar("T")


def address_topic(address: str) -> str:
    """An address as a 32-byte log topic."""
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


class X402PaymentAdapter(PaymentAdapter):
    name = "X402PaymentAdapter"

    def __init__(
        self,
        rail_id: str,
        rail: RailConfig,
        environ: Mapping[str, str],
        *,
        rpc: EvmRpcClient | None = None,
        facilitator: FacilitatorClient | None = None,
    ) -> None:
        super().__init__(rail_id, rail, environ)
        if (
            rail.protocol != "x402"
            or rail.scheme != "exact"
            or rail.x402 is None
            or rail.evm_chain_id is None
            or rail.token_contract is None
        ):
            raise ValueError(f"rail {rail_id} is not a complete x402 'exact' rail on an EVM chain")
        self.chain_id: int = rail.evm_chain_id
        self.token: str = rail.token_contract
        self.settings = rail.x402
        self._rpc = rpc
        self._facilitator = facilitator
        self._ready = False

    # ------------------------------------------------------------------ services

    def _env_url(self, name: str) -> str:
        value = (self.environ.get(name) or "").strip()
        if not value:
            raise AdapterUnavailable(f"{name} is empty")
        return value

    def rpc(self) -> EvmRpcClient:
        if self._rpc is None:
            try:
                self._rpc = EvmRpcClient(self._env_url(self.settings.rpc_url_env))
            except ValueError as exc:
                raise AdapterUnavailable(str(exc)) from exc
        return self._rpc

    def facilitator(self) -> FacilitatorClient:
        if self._facilitator is None:
            try:
                self._facilitator = FacilitatorClient(
                    self._env_url(self.settings.facilitator_url_env)
                )
            except ValueError as exc:
                raise AdapterUnavailable(str(exc)) from exc
        return self._facilitator

    def _chain(self, read: Callable[[EvmRpcClient], T]) -> T:
        try:
            return read(self.rpc())
        except (EvmRpcUnavailable, EvmRpcError) as exc:
            raise AdapterUnavailable(str(exc)) from exc

    @property
    def pay_to(self) -> str:
        address = self.configured_recipient()
        if not EVM_ADDRESS.match(address):
            raise AdapterUnavailable(f"{self.rail.recipient.env} is not an EVM address")
        return address

    def check_ready(self) -> None:
        chain_id = self._chain(lambda rpc: rpc.chain_id())
        if chain_id != self.chain_id:
            raise AdapterUnavailable(
                f"the chain RPC is on chain {chain_id}, rail {self.rail_id} expects {self.chain_id}"
            )
        code = self._chain(lambda rpc: rpc.code(self.token))
        if code.lower() in ("", "0x", "0x0"):
            raise AdapterUnavailable(f"no token contract at {self.token} on chain {chain_id}")
        try:
            supported = self.facilitator().supports("exact", self.rail.network)
        except FacilitatorUnavailable as exc:
            raise AdapterUnavailable(str(exc)) from exc
        if not supported:
            raise AdapterUnavailable(
                f"the x402 facilitator does not support 'exact' payments on {self.rail.network}"
            )
        self._ready = True

    def _ensure_ready(self) -> None:
        if not self._ready:
            self.check_ready()

    # ------------------------------------------------------------------ quotes

    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        self._ensure_ready()
        return PaymentDestination(
            recipient=self.pay_to,
            recipient_kind="shared_with_reference",
            payment_reference=payment_intent_id,
        )

    def payment_requirements(
        self, amount_base_units: int, pay_to: str, max_timeout_seconds: int
    ) -> dict[str, Any]:
        """The x402 ``PaymentRequirements`` for one intent (what the payer signs for)."""
        return {
            "scheme": "exact",
            "network": self.rail.network,
            "amount": str(amount_base_units),
            "asset": self.token,
            "payTo": pay_to,
            "maxTimeoutSeconds": max(1, min(max_timeout_seconds, MAX_TIMEOUT_SECONDS)),
            "extra": {
                "name": self.settings.asset_eip712_name,
                "version": self.settings.asset_eip712_version,
            },
        }

    def current_block(self) -> int:
        return self._chain(lambda rpc: rpc.block_number())

    # ------------------------------------------------------------------ observation

    def observe(self, expected: ExpectedPayment) -> PaymentObservation:
        now = datetime.now(UTC)
        if not expected.authorizations:
            # Nothing was submitted for this intent, so there is nothing to look for.
            return PaymentObservation(expected.payment_intent_id, self.name, now)
        tip = self._chain(lambda rpc: rpc.block_number())
        if self._chain(lambda rpc: rpc.chain_id()) != self.chain_id:
            self._ready = False
            raise AdapterUnavailable("the chain RPC changed chain; refusing to observe payments")

        outputs: dict[tuple[str, int], ObservedOutput] = {}
        reorged: list[str] = []
        notes: list[str] = []
        recipient = expected.destination.recipient
        for authorization in expected.authorizations:
            # The transaction the facilitator reported is checked first. If it does not show
            # the payment, the chain's own record of the authorization's use is searched,
            # which also finds the payment after a reorganization moved it to another block.
            checked: list[tuple[str, str]] = []
            found: list[ObservedOutput] = []
            if authorization.transaction_hint:
                hint = authorization.transaction_hint.lower()
                found, state = self._verify_transaction(hint, authorization, recipient, tip)
                checked.append((hint, state))
            if not found:
                for transaction in self._search(authorization, tip):
                    if any(transaction == done for done, _ in checked):
                        continue
                    more, state = self._verify_transaction(
                        transaction, authorization, recipient, tip
                    )
                    checked.append((transaction, state))
                    found.extend(more)
            for output in found:
                outputs[(output.transaction_reference, output.output_index)] = output
            if not found:
                for transaction, state in checked:
                    if state == "reorged":
                        reorged.append(transaction)
                    elif state != "ok":
                        notes.append(f"{transaction}: {state}")
        return PaymentObservation(
            payment_intent_id=expected.payment_intent_id,
            adapter=self.name,
            observed_at=now,
            outputs=tuple(outputs.values()),
            reorged_transactions=tuple(dict.fromkeys(reorged)),
            tip_height=tip,
            notes=tuple(notes),
        )

    def _search(self, authorization: SubmittedAuthorization, tip: int) -> list[str]:
        """Transactions in which the token contract recorded the use of this authorization."""
        start = authorization.search_from_block
        if start is None:
            start = max(0, tip - LOG_SEARCH_BLOCKS)
        if start > tip:
            return []
        end = min(tip, start + LOG_SEARCH_BLOCKS)
        topics: list[str | None] = [
            AUTHORIZATION_USED_TOPIC,
            address_topic(authorization.payer),
            authorization.nonce.lower(),
        ]
        logs = self._chain(lambda rpc: rpc.logs(self.token, topics, start, end))
        found = [
            str(log["transactionHash"]).lower()
            for log in logs
            if not log.get("removed") and isinstance(log.get("transactionHash"), str)
        ]
        return list(dict.fromkeys(found))

    def _verify_transaction(
        self,
        transaction: str,
        authorization: SubmittedAuthorization,
        recipient: str,
        tip: int,
    ) -> tuple[list[ObservedOutput], str]:
        receipt = self._chain(lambda rpc: rpc.receipt(transaction))
        if receipt is None:
            return [], "not in a block (pending or dropped)"
        if receipt.get("status") != "0x1":
            return [], "transaction failed on chain (no transfer)"
        try:
            block_number = quantity(receipt.get("blockNumber"), "blockNumber")
        except EvmRpcUnavailable as exc:
            raise AdapterUnavailable(f"unreadable receipt for {transaction}") from exc
        block_hash = str(receipt.get("blockHash") or "").lower()
        block = self._chain(lambda rpc: rpc.block(block_number))
        if block is None or str(block.get("hash") or "").lower() != block_hash:
            return [], "reorged"

        token = self.token.lower()
        payer = address_topic(authorization.payer)
        logs = [
            log for log in receipt.get("logs") or [] if str(log.get("address")).lower() == token
        ]
        used = any(
            _topics(log)[:3] == [AUTHORIZATION_USED_TOPIC, payer, authorization.nonce.lower()]
            for log in logs
        )
        if not used:
            return [], "does not use this payment authorization"

        first_seen = datetime.fromtimestamp(quantity(block.get("timestamp"), "timestamp"), UTC)
        confirmations = max(1, tip - block_number + 1)
        found: list[ObservedOutput] = []
        for log in logs:
            if _topics(log) != [TRANSFER_TOPIC, payer, address_topic(recipient)]:
                continue
            try:
                amount = int(str(log.get("data")), 16)
                index = quantity(log.get("logIndex"), "logIndex")
            except (ValueError, EvmRpcUnavailable) as exc:
                raise AdapterUnavailable(f"unreadable transfer in {transaction}") from exc
            if amount > 0:
                found.append(
                    ObservedOutput(
                        transaction_reference=transaction,
                        output_index=index,
                        amount_base_units=amount,
                        confirmations=confirmations,
                        first_seen_at=first_seen,
                        block_hash=block_hash,
                        block_height=block_number,
                    )
                )
        return found, "ok" if found else "no transfer to the receiving address"


def _topics(log: Mapping[str, Any]) -> list[str]:
    return [str(topic).lower() for topic in log.get("topics") or []]
