"""End to end against a real DebitMyData Core node in regtest mode.

Skipped unless these are set (plus TEST_DATABASE_URL):

    DMD_REGTEST_RPC_URL=http://127.0.0.1:58557
    DMD_REGTEST_RPC_USER=...        # an RPC user allowed to create wallets and mine
    DMD_REGTEST_RPC_PASSWORD=...

The test creates its own fresh wallets (payer, treasury and a watch-only gateway wallet),
mines test coins, and then runs the real flow: quote -> payment -> worker -> settlement,
plus a chain reorganization and a double-spend. Never point this at a mainnet node.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from dmd_gateway.db.session import session_factory
from dmd_gateway.main import create_app
from dmd_gateway.services.settlement import run_pass
from dmd_gateway.settings import Settings

from .conftest import REPO_ROOT, TEST_DATABASE_URL, alembic_config

RPC_URL = os.environ.get("DMD_REGTEST_RPC_URL")
RPC_USER = os.environ.get("DMD_REGTEST_RPC_USER")
RPC_PASSWORD = os.environ.get("DMD_REGTEST_RPC_PASSWORD")

pytestmark = pytest.mark.skipif(
    not (RPC_URL and RPC_USER and RPC_PASSWORD and TEST_DATABASE_URL),
    reason="DMD regtest node or TEST_DATABASE_URL not configured",
)

URL = "/v1/dids/registration-quotes"
TEST_PRICE = "2500"  # USD per DMD in this test: the 25.00 USD fee is 0.01 DMD


class AdminRpc:
    """Unrestricted RPC for test setup only (mining, sending, invalidating blocks)."""

    def __init__(self, wallet: str | None = None) -> None:
        self.url = RPC_URL.rstrip("/") + (f"/wallet/{wallet}" if wallet else "")

    def __call__(self, method: str, *params: Any) -> Any:
        response = httpx.post(
            self.url,
            auth=(RPC_USER, RPC_PASSWORD),
            json={"jsonrpc": "1.0", "id": 1, "method": method, "params": list(params)},
            timeout=60,
        )
        body = json.loads(response.content, parse_float=Decimal)
        if body.get("error"):
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]


@pytest.fixture(scope="module")
def node() -> dict[str, Any]:
    root = AdminRpc()
    if root("getblockchaininfo")["chain"] != "regtest":
        pytest.skip("node is not on regtest")
    suffix = secrets.token_hex(4)
    payer, treasury, watch = f"e2e-payer-{suffix}", f"e2e-treasury-{suffix}", f"e2e-gw-{suffix}"
    root("createwallet", payer)
    root("createwallet", treasury)
    root("createwallet", watch, True, True)  # disable_private_keys, blank
    descriptor = next(
        d["desc"]
        for d in AdminRpc(treasury)("listdescriptors")["descriptors"]
        if d["desc"].startswith("wpkh(") and not d["internal"]
    )
    AdminRpc(watch)(
        "importdescriptors",
        [
            {
                "desc": descriptor,
                "timestamp": "now",
                "active": True,
                "internal": False,
                "range": [0, 200],
            }
        ],
    )
    payer_rpc = AdminRpc(payer)
    mine_to = payer_rpc("getnewaddress")
    root("generatetoaddress", 260, mine_to)  # regtest coinbase matures after 250 blocks
    for _ in range(10):  # block rewards halve every 150 blocks on regtest
        if payer_rpc("getbalance") >= Decimal("0.2"):
            break
        root("generatetoaddress", 50, mine_to)
    else:
        pytest.skip("regtest chain too long for meaningful rewards; use a fresh regtest datadir")
    return {"root": root, "payer": payer_rpc, "mine_to": mine_to, "wallet": watch}


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()
    command.downgrade(config, "base")


@pytest.fixture(scope="module")
def client(node: dict[str, Any], engine: Engine) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        route_registry_path=REPO_ROOT / "config" / "payment_routes.regtest.yaml",
    )
    environ = {
        "DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true",
        "DMD_RPC_URL": RPC_URL,
        "DMD_RPC_USER": RPC_USER,
        "DMD_RPC_PASSWORD": RPC_PASSWORD,
        "DMD_RPC_WALLET": node["wallet"],
        "DMD_STATIC_TEST_PRICE_USD": TEST_PRICE,
    }
    with TestClient(create_app(settings, environ=environ, engine=engine)) as test_client:
        yield test_client


def new_quote(client: TestClient) -> dict[str, Any]:
    body = {
        "display_name": "RegtestAgent-01",
        "participant_type": "autonomous_agent",
        "controller_public_key": "ed25519:REGTEST_PUBLIC_KEY",
        "preferred_payment_option": "dmd_pow_native",
    }
    response = client.post(URL, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def status_of(client: TestClient, payment_id: str) -> dict[str, Any]:
    return client.get(f"/v1/payments/{payment_id}").json()


def wait_for(client: TestClient, payment_id: str, wanted: str, tries: int = 30) -> dict[str, Any]:
    state = client.app.state
    factory = session_factory(state.engine)
    for _ in range(tries):
        run_pass(factory, state.quote_context.adapters, datetime.now(UTC))
        current = status_of(client, payment_id)
        if current["status"] == wanted:
            return current
        time.sleep(0.3)
    raise AssertionError(f"expected {wanted}, still {current['status']}")


def mine(node: dict[str, Any], blocks: int) -> list[str]:
    return node["root"]("generatetoaddress", blocks, node["mine_to"])


def test_quote_uses_a_fresh_watch_only_address(client: TestClient) -> None:
    first, second = new_quote(client), new_quote(client)
    a = first["selected_payment_option"]
    assert a["recipient"].startswith("dmrt1")
    assert a["recipient"] != second["selected_payment_option"]["recipient"]
    assert a["amount"] == "0.01000000"
    assert a["network"] == "dmd:regtest"
    assert a["required_confirmations"] == 6


def test_payment_settles_after_six_confirmations(
    client: TestClient, node: dict[str, Any], engine: Engine
) -> None:
    q = new_quote(client)
    pid = q["payment_intent_id"]
    option = q["selected_payment_option"]
    txid = node["payer"]("sendtoaddress", option["recipient"], option["amount"])

    s = wait_for(client, pid, "confirming")
    assert s["transactions"] == [txid]
    assert s["confirmations"] == 0

    mine(node, 1)
    assert wait_for(client, pid, "verified")["confirmations"] == 1

    mine(node, 5)
    s = wait_for(client, pid, "settled")
    assert s["confirmations"] == 6
    assert s["amount_received"] == "0.01000000"
    with engine.connect() as conn:
        credited = (
            conn.execute(
                text(
                    "SELECT transaction_reference FROM credited_outputs"
                    " WHERE payment_intent_id = :p"
                ),
                {"p": pid},
            )
            .scalars()
            .all()
        )
    assert credited == [txid]


def test_reorg_reopens_and_resettles(client: TestClient, node: dict[str, Any]) -> None:
    q = new_quote(client)
    pid = q["payment_intent_id"]
    option = q["selected_payment_option"]
    node["payer"]("sendtoaddress", option["recipient"], option["amount"])
    wait_for(client, pid, "confirming")
    blocks = mine(node, 6)
    wait_for(client, pid, "settled")

    node["root"]("invalidateblock", blocks[0])  # disconnect the block holding the payment
    s = wait_for(client, pid, "confirming")
    assert s["confirmations"] == 0

    node["root"]("reconsiderblock", blocks[0])
    wait_for(client, pid, "settled")


def test_double_spend_fails_the_payment(client: TestClient, node: dict[str, Any]) -> None:
    q = new_quote(client)
    pid = q["payment_intent_id"]
    option = q["selected_payment_option"]
    payer = node["payer"]
    # One coin of 0.05 DMD to double-spend (regtest rewards may be split into tiny coins).
    funding = payer("sendtoaddress", payer("getnewaddress"), "0.05")
    mine(node, 1)
    coin = next(
        u
        for u in payer("listunspent", 1)
        if u["txid"] == funding and u["amount"] == Decimal("0.05")
    )
    change = payer("getrawchangeaddress")

    def spend(outputs: dict[str, Any]) -> str:
        raw = payer("createrawtransaction", [{"txid": coin["txid"], "vout": coin["vout"]}], outputs)
        return payer("sendrawtransaction", payer("signrawtransactionwithwallet", raw)["hex"])

    rest = coin["amount"] - Decimal(option["amount"]) - Decimal("0.0001")
    spend({option["recipient"]: option["amount"], change: str(rest)})
    wait_for(client, pid, "confirming")

    # Same coin, paid back to the payer with a higher fee: replaces the payment in the mempool.
    spend({change: str(coin["amount"] - Decimal("0.001"))})
    assert wait_for(client, pid, "failed")["status"] == "failed"
