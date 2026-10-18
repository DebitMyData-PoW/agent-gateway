"""Native DMD adapter and its restricted RPC client, against a simulated node."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest
import yaml

from dmd_gateway.adapters.base import AdapterUnavailable, ExpectedPayment, PaymentDestination
from dmd_gateway.adapters.dmd_native import DMDNativePaymentAdapter
from dmd_gateway.adapters.dmd_rpc import DmdRpcClient, MethodNotAllowed
from dmd_gateway.domain.routes import RailConfig

from .conftest import REPO_ROOT

ADDRESS = "dmrt1qj4eksj3exat9jqryakw4066lacqrnq2a6qmlx3"
TXID = "e1631f545874987c3bb3562ee3284d527d65cc6296d37c41385e81f09a9869fb"
BLOCK = "16bb2a381bcba2d6f9b975e9f7167ce5c9ccb3c91fb4e194c2494372c6ef314d"


def regtest_rail() -> RailConfig:
    data = yaml.safe_load((REPO_ROOT / "config" / "payment_routes.regtest.yaml").read_text())
    return RailConfig.model_validate(data["rails"]["dmd_pow_native"])


class FakeNode:
    """Answers JSON-RPC like DMD Core v1.3.3 for one watch-only wallet."""

    def __init__(self) -> None:
        self.chain = "regtest"
        self.private_keys_enabled = False
        self.calls: list[str] = []
        self.txs: dict[str, dict[str, Any]] = {}
        self.headers: dict[str, int] = {}
        self.raw: dict[str, dict[str, Any]] = {}
        self.listed: list[str] = []

    def add_payment(
        self,
        amount: str,
        *,
        txid: str = TXID,
        confirmations: int = 1,
        wallet_amount: str | None = None,
        coinbase: bool = False,
    ) -> None:
        self.listed.append(txid)
        self.txs[txid] = {
            "txid": txid,
            "confirmations": confirmations,
            "blockhash": BLOCK if confirmations > 0 else None,
            "blockheight": 261 if confirmations > 0 else None,
            "time": 1792340900,
            "timereceived": 1792340900,
            "hex": txid,  # the fake decodes by txid
            "details": [
                {
                    "address": ADDRESS,
                    "category": "receive",
                    "amount": wallet_amount or amount,
                    "vout": 1,
                }
            ],
        }
        self.headers[BLOCK] = confirmations if confirmations > 0 else -1
        vin = [{"coinbase": "01"}] if coinbase else [{"txid": "00" * 32, "vout": 0}]
        self.raw[txid] = {
            "txid": txid,
            "vin": vin,
            "vout": [
                {"n": 0, "value": "48.49", "scriptPubKey": {"address": "dmrt1qsomeoneelse"}},
                {"n": 1, "value": amount, "scriptPubKey": {"address": ADDRESS}},
            ],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        result: Any
        if method == "getblockchaininfo":
            result = {"chain": self.chain, "blocks": 261, "initialblockdownload": False}
        elif method == "getwalletinfo":
            result = {"private_keys_enabled": self.private_keys_enabled, "descriptors": True}
        elif method == "getnewaddress":
            result = ADDRESS
        elif method == "getaddressinfo":
            result = {"address": params["address"], "ismine": True, "ischange": False}
        elif method == "listreceivedbyaddress":
            result = [{"address": ADDRESS, "amount": 0, "txids": list(self.listed)}]
        elif method == "gettransaction":
            if params["txid"] not in self.txs:
                return self._error(-5, "Invalid or non-wallet transaction id")
            result = self.txs[params["txid"]]
        elif method == "getblockheader":
            result = {"confirmations": self.headers[params["blockhash"]], "height": 261}
        elif method == "decoderawtransaction":
            result = self.raw[params["hexstring"]]
        else:
            return self._error(-32601, "Method not found")
        # Amounts as JSON numbers, exactly like the node sends them.
        text = json.dumps({"result": result, "error": None, "id": body["id"]})
        text = text.replace('"value": "', '"value": ').replace('"amount": "', '"amount": ')
        text = _unquote_numbers(text)
        return httpx.Response(200, content=text)

    @staticmethod
    def _error(code: int, message: str) -> httpx.Response:
        payload = {"result": None, "error": {"code": code, "message": message}, "id": 1}
        return httpx.Response(500, json=payload)


def _unquote_numbers(text: str) -> str:
    # Turn  "value": 1.50000000"  back into a bare JSON number.
    return re.sub(r'("(?:value|amount)": )(-?[0-9.]+)"', r"\1\2", text)


def make_adapter(node: FakeNode) -> DMDNativePaymentAdapter:
    client = DmdRpcClient(
        "http://127.0.0.1:58557",
        "gateway",
        "secret",
        "gateway-receive",
        transport=httpx.MockTransport(node.handle),
    )
    return DMDNativePaymentAdapter("dmd_pow_native", regtest_rail(), {}, rpc=client)


def expected(units: int = 150_000_000, known: tuple[str, ...] = ()) -> ExpectedPayment:
    return ExpectedPayment(
        payment_intent_id="dmd_payment_intent_X",
        network="dmd:regtest",
        asset_canonical_id="dmd:regtest/native",
        amount_base_units=units,
        destination=PaymentDestination(ADDRESS, "per_intent_address", "dmd_payment_intent_X"),
        required_confirmations=6,
        known_transactions=known,
    )


# ------------------------------------------------------------------ RPC client


def test_only_read_only_methods_can_be_called() -> None:
    client = make_adapter(FakeNode())._rpc
    assert client is not None
    for method in ("sendtoaddress", "dumpprivkey", "walletpassphrase", "stop", "generate"):
        with pytest.raises(MethodNotAllowed):
            client.call(method)


@pytest.mark.parametrize(
    "url",
    ["http://8.8.8.8:58557", "http://node.example.com:18557", "http://user:pw@127.0.0.1:1"],
)
def test_rpc_must_be_private(url: str) -> None:
    with pytest.raises(ValueError):
        DmdRpcClient(url, "u", "p", "w")


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:58557", "http://10.1.2.3:18557", "http://localhost:1"]
)
def test_private_rpc_urls_are_accepted(url: str) -> None:
    DmdRpcClient(url, "u", "p", "w").close()


def test_missing_rpc_settings_make_the_rail_unavailable() -> None:
    adapter = DMDNativePaymentAdapter("dmd_pow_native", regtest_rail(), {})
    with pytest.raises(AdapterUnavailable, match="DMD_RPC_URL"):
        adapter.check_ready()


def test_amounts_are_decimals_not_floats() -> None:
    node = FakeNode()
    node.add_payment("1.50000000")
    adapter = make_adapter(node)
    tx = adapter._rpc.call("gettransaction", {"txid": TXID})  # type: ignore[union-attr]
    assert isinstance(tx["details"][0]["amount"], Decimal)


# ------------------------------------------------------------------ safety checks


def test_refuses_a_wallet_that_can_spend() -> None:
    node = FakeNode()
    node.private_keys_enabled = True
    with pytest.raises(AdapterUnavailable, match="watch-only"):
        make_adapter(node).check_ready()


def test_refuses_a_node_on_another_chain() -> None:
    node = FakeNode()
    node.chain = "main"
    with pytest.raises(AdapterUnavailable, match="expects 'regtest'"):
        make_adapter(node).check_ready()


def test_fresh_address_per_intent() -> None:
    node = FakeNode()
    destination = make_adapter(node).prepare_destination("dmd_payment_intent_X")
    assert destination.recipient == ADDRESS
    assert destination.recipient_kind == "per_intent_address"
    assert destination.payment_reference == "dmd_payment_intent_X"
    assert "getnewaddress" in node.calls


# ------------------------------------------------------------------ observation


def test_confirmed_payment_is_verified_from_the_raw_transaction() -> None:
    node = FakeNode()
    node.add_payment("1.50000000", confirmations=1)
    observation = make_adapter(node).observe(expected())
    (output,) = observation.outputs
    assert output.transaction_reference == TXID
    assert output.output_index == 1  # only the output paying our address
    assert output.amount_base_units == 150_000_000
    assert output.confirmations == 1
    assert output.block_hash == BLOCK
    assert {"decoderawtransaction", "getblockheader"} <= set(node.calls)


def test_mempool_payment_has_zero_confirmations() -> None:
    node = FakeNode()
    node.add_payment("1.5", confirmations=0)
    (output,) = make_adapter(node).observe(expected()).outputs
    assert output.confirmations == 0
    assert output.block_hash is None


def test_block_that_left_the_chain_is_reported_as_reorged() -> None:
    node = FakeNode()
    node.add_payment("1.5", confirmations=3)
    node.headers[BLOCK] = -1  # wallet still points at a stale block
    observation = make_adapter(node).observe(expected())
    assert observation.reorged_transactions == (TXID,)
    assert observation.outputs[0].confirmations == 0


def test_double_spent_transaction_is_reported_as_conflicted() -> None:
    node = FakeNode()
    node.add_payment("1.5", confirmations=0)
    node.txs[TXID]["confirmations"] = -2
    node.listed.clear()  # the wallet no longer lists it as received
    observation = make_adapter(node).observe(expected(known=(TXID,)))
    assert observation.outputs == ()
    assert observation.conflicted_transactions == (TXID,)


def test_replaced_mempool_transaction_is_reported_as_conflicted() -> None:
    node = FakeNode()
    node.add_payment("1.5", confirmations=0)
    node.txs[TXID]["mempoolconflicts"] = ["cd" * 32]
    observation = make_adapter(node).observe(expected())
    assert observation.outputs == ()
    assert observation.conflicted_transactions == (TXID,)


def test_wallet_and_raw_transaction_must_agree() -> None:
    node = FakeNode()
    node.add_payment("1.5", wallet_amount="15.0")
    with pytest.raises(AdapterUnavailable, match="disagree"):
        make_adapter(node).observe(expected())


def test_coinbase_outputs_are_ignored() -> None:
    node = FakeNode()
    node.add_payment("50", coinbase=True)
    observation = make_adapter(node).observe(expected())
    assert observation.outputs == ()


def test_payer_hint_counts_only_if_the_wallet_sees_it() -> None:
    node = FakeNode()
    hint_expected = replace(expected(), payer_reference_hint="ff" * 32)
    assert make_adapter(node).observe(hint_expected).outputs == ()


def test_chain_switch_stops_observation() -> None:
    node = FakeNode()
    adapter = make_adapter(node)
    adapter.check_ready()
    node.chain = "main"
    with pytest.raises(AdapterUnavailable, match="changed chain"):
        adapter.observe(expected())


def test_node_down_is_unavailable_not_an_error() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = DmdRpcClient(
        "http://127.0.0.1:58557", "u", "p", "w", transport=httpx.MockTransport(refuse)
    )
    adapter = DMDNativePaymentAdapter("dmd_pow_native", regtest_rail(), {}, rpc=client)
    with pytest.raises(AdapterUnavailable, match="unreachable"):
        adapter.check_ready()
