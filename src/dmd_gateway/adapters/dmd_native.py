"""Native DMD payment adapter (DMD-01..DMD-15).

How it works:
  * The gateway's node wallet is a watch-only descriptor wallet: it can derive receive
    addresses and see payments, but holds no private keys (SEC-08, SEC-09). The adapter
    refuses to run if the wallet can spend, or if the node is on a different chain than
    the rail (e.g. a regtest node behind a mainnet rail).
  * Every quote gets a fresh address from the wallet, labelled with the payment-intent ID
    (DMD-02, DMD-15).
  * To observe a payment, the adapter lists the wallet transactions paying that address,
    then verifies each one independently: it decodes the raw transaction and checks the
    output script and amount itself, and checks that the confirming block is still in the
    active chain (DMD-04, DMD-05, DMD-08). Conflicted (double-spent) transactions are
    reported separately (DMD-09).
  * A transaction reference sent by the payer is only a hint; it counts only if the node
    independently shows it paying the expected address (DMD-12).

Only read-only RPC methods are used; see ``dmd_rpc.ALLOWED_METHODS``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from dmd_gateway.adapters.base import (
    AdapterUnavailable,
    ExpectedPayment,
    ObservedOutput,
    PaymentAdapter,
    PaymentDestination,
    PaymentObservation,
)
from dmd_gateway.adapters.dmd_rpc import (
    RPC_INVALID_ADDRESS_OR_KEY,
    DmdRpcClient,
    RpcError,
    RpcUnavailable,
)
from dmd_gateway.domain.money import AmountError, to_base_units
from dmd_gateway.domain.routes import RailConfig

log = logging.getLogger(__name__)

# rail network -> (chain name reported by getblockchaininfo, bech32 address prefix)
NETWORKS: dict[str, tuple[str, str]] = {
    "dmd:mainnet": ("main", "dm"),
    "dmd:testnet": ("test", "td"),
    "dmd:testnet4": ("testnet4", "td"),
    "dmd:signet": ("signet", "td"),
    "dmd:regtest": ("regtest", "dmrt"),
}


class DMDNativePaymentAdapter(PaymentAdapter):
    name = "DMDNativePaymentAdapter"

    def __init__(
        self,
        rail_id: str,
        rail: RailConfig,
        environ: Mapping[str, str],
        *,
        rpc: DmdRpcClient | None = None,
    ) -> None:
        super().__init__(rail_id, rail, environ)
        if rail.network not in NETWORKS:
            raise ValueError(f"unsupported DebitMyData network {rail.network!r}")
        self._chain, self._hrp = NETWORKS[rail.network]
        self._rpc = rpc
        self._ready = False

    # ------------------------------------------------------------------ node access

    def _client(self) -> DmdRpcClient:
        if self._rpc is None:
            try:
                self._rpc = DmdRpcClient.from_environ(self.environ)
            except (RpcUnavailable, ValueError) as exc:
                raise AdapterUnavailable(str(exc)) from exc
        return self._rpc

    def _call(self, method: str, **params: Any) -> Any:
        try:
            return self._client().call(method, params)
        except RpcUnavailable as exc:
            raise AdapterUnavailable(str(exc)) from exc

    def check_ready(self) -> None:
        chain = self._call("getblockchaininfo")
        if chain.get("chain") != self._chain:
            raise AdapterUnavailable(
                f"DebitMyData node is on chain {chain.get('chain')!r}, rail {self.rail_id} "
                f"expects {self._chain!r}"
            )
        if chain.get("initialblockdownload"):
            raise AdapterUnavailable("DebitMyData node is still syncing (initial block download)")
        wallet = self._call("getwalletinfo")
        if wallet.get("private_keys_enabled") is not False:
            raise AdapterUnavailable(
                "the gateway's receiving wallet can spend funds; it must be a watch-only wallet"
            )
        if wallet.get("descriptors") is not True:
            raise AdapterUnavailable("the gateway's receiving wallet must be a descriptor wallet")
        self._ready = True

    def _ensure_ready(self) -> None:
        if not self._ready:
            self.check_ready()

    # ------------------------------------------------------------------ destinations

    def prepare_destination(self, payment_intent_id: str) -> PaymentDestination:
        self._ensure_ready()
        address = self._call("getnewaddress", label=payment_intent_id, address_type="bech32")
        if not isinstance(address, str) or not address.startswith(self._hrp + "1"):
            raise AdapterUnavailable(
                "DebitMyData node returned an address for an unexpected network"
            )
        info = self._call("getaddressinfo", address=address)
        if info.get("ismine") is not True or info.get("ischange") is True:
            raise AdapterUnavailable(
                "DebitMyData node returned an address the wallet does not watch"
            )
        return PaymentDestination(
            recipient=address,
            recipient_kind="per_intent_address",
            payment_reference=payment_intent_id,
        )

    # ------------------------------------------------------------------ observation

    def observe(self, expected: ExpectedPayment) -> PaymentObservation:
        self._ensure_ready()
        address = expected.destination.recipient
        chain = self._call("getblockchaininfo")
        if chain.get("chain") != self._chain:
            self._ready = False
            raise AdapterUnavailable("DebitMyData node changed chain; refusing to observe payments")

        listed = self._call(
            "listreceivedbyaddress", minconf=0, include_empty=True, address_filter=address
        )
        txids: list[str] = []
        for entry in listed or []:
            if entry.get("address") == address:
                txids.extend(entry.get("txids") or [])
        txids.extend(expected.known_transactions)
        if expected.payer_reference_hint:
            txids.append(expected.payer_reference_hint)

        outputs: list[ObservedOutput] = []
        conflicted: list[str] = []
        reorged: list[str] = []
        notes: list[str] = []
        for txid in dict.fromkeys(txids):
            try:
                tx = self._call("gettransaction", txid=txid)
            except RpcError as exc:
                if exc.code == RPC_INVALID_ADDRESS_OR_KEY:
                    continue  # not a wallet transaction (e.g. a bogus payer hint)
                raise AdapterUnavailable(str(exc)) from exc
            found, state = self._verify_transaction(tx, txid, address)
            if state == "conflicted":
                conflicted.append(txid)
            elif state == "reorged":
                reorged.append(txid)
            elif state == "skipped":
                notes.append(f"{txid}: not a spendable payment (coinbase)")
            outputs.extend(found)

        return PaymentObservation(
            payment_intent_id=expected.payment_intent_id,
            adapter=self.name,
            observed_at=datetime.now(UTC),
            outputs=tuple(outputs),
            conflicted_transactions=tuple(conflicted),
            reorged_transactions=tuple(reorged),
            tip_height=chain.get("blocks"),
            notes=tuple(notes),
        )

    def _verify_transaction(
        self, tx: Mapping[str, Any], txid: str, address: str
    ) -> tuple[list[ObservedOutput], str]:
        confirmations = int(tx.get("confirmations", 0))
        abandoned = any(d.get("abandoned") for d in tx.get("details") or [])
        # Negative confirmations: a conflicting transaction is in the chain. A conflicting
        # transaction in the mempool means ours was replaced (double-spend attempt).
        replaced = confirmations == 0 and bool(tx.get("mempoolconflicts"))
        if confirmations < 0 or abandoned or replaced:
            return [], "conflicted"

        block_hash = tx.get("blockhash")
        block_height = tx.get("blockheight")
        state = "ok"
        if confirmations > 0:
            header = self._call("getblockheader", blockhash=block_hash, verbose=True)
            header_confirmations = int(header.get("confirmations", -1))
            if header_confirmations < 1:
                # The wallet still points at a block that is no longer in the active chain.
                confirmations, block_hash, block_height, state = 0, None, None, "reorged"
            else:
                confirmations = header_confirmations
                block_height = header.get("height", block_height)

        decoded = self._call("decoderawtransaction", hexstring=tx["hex"])
        if decoded.get("txid") != txid:
            raise AdapterUnavailable(f"raw transaction does not match txid {txid}")
        if any("coinbase" in vin for vin in decoded.get("vin") or []):
            return [], "skipped"

        decimals = self.rail.asset.decimals
        first_seen = datetime.fromtimestamp(int(tx.get("timereceived") or tx["time"]), UTC)
        found: list[ObservedOutput] = []
        for vout in decoded.get("vout") or []:
            if (vout.get("scriptPubKey") or {}).get("address") != address:
                continue
            try:
                units = to_base_units(Decimal(vout["value"]), decimals)
            except (AmountError, KeyError) as exc:
                raise AdapterUnavailable(f"unreadable output amount in {txid}") from exc
            found.append(
                ObservedOutput(
                    transaction_reference=txid,
                    output_index=int(vout["n"]),
                    amount_base_units=units,
                    confirmations=confirmations,
                    first_seen_at=first_seen,
                    block_hash=block_hash,
                    block_height=int(block_height) if block_height is not None else None,
                )
            )

        # Defence in depth: the wallet's own accounting must agree with the raw transaction.
        wallet_units = 0
        for detail in tx.get("details") or []:
            if detail.get("category") == "receive" and detail.get("address") == address:
                wallet_units += to_base_units(Decimal(detail["amount"]), decimals)
        if wallet_units != sum(o.amount_base_units for o in found):
            raise AdapterUnavailable(f"wallet and raw transaction disagree for {txid}")
        return found, state
