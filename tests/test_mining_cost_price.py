"""The DMD/USD mining-cost price: the chain rules, the formula, its public inputs, storage, the
worker's refresher and the public endpoint.

The formula and the inputs need no database; storing, quoting and the endpoint use the
disposable PostgreSQL database named by TEST_DATABASE_URL and are skipped without it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from dmd_gateway.db.session import session_factory
from dmd_gateway.main import create_app
from dmd_gateway.pricing import mining_cost as mc
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL, UNREACHABLE_DB, alembic_config

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

# The explorer's figures on October 24, 2026, and Bitcoin's at the end of September 2026.
DMD_HASHRATE = Decimal("46227196906926.34")
DMD_HEIGHT = 135_879
BTC_PRICE = Decimal("83479")
BTC_HEIGHT = 918_000
BTC_HASHPRICE_PER_PH = Decimal("39.87")


# --------------------------------------------------------------------------- the formula


@pytest.mark.parametrize(
    ("height", "subsidy"),
    [
        (0, "35"),
        (135_879, "35"),
        (299_999, "35"),
        (300_000, "17.5"),
        (600_000, "8.75"),
        (9_599_999, "0.00000001"),
        (9_600_000, "35"),
    ],
)
def test_dmd_subsidy_follows_the_chain_rule(height: int, subsidy: str) -> None:
    # 35 DMD halving every 300,000 blocks, the halvings restarting every 9,600,000 blocks;
    # amounts are whole data (1e-8 DMD), as in the node.
    assert mc.dmd_block_subsidy(height) == Decimal(subsidy)


def test_btc_subsidy() -> None:
    assert mc.btc_block_subsidy(840_000) == Decimal("3.125")
    assert mc.btc_block_subsidy(1_050_000) == Decimal("1.5625")


def test_todays_figures_give_about_nine_thousandths_of_a_cent() -> None:
    hashprice = BTC_HASHPRICE_PER_PH / Decimal(10) ** 15  # USD per hash/s per day
    price = mc.dmd_mining_cost_usd(hashprice, DMD_HASHRATE, Decimal(35))
    assert Decimal("0.0000913") < price < Decimal("0.0000915")
    assert 273_000 < Decimal(25) / price < 274_000  # DMD for a $25 DID


def test_hashprice_from_price_and_difficulty() -> None:
    # 952 EH/s of Bitcoin hashrate is a difficulty of 952e18 × 600 / 2^32.
    difficulty = Decimal(952) * Decimal(10) ** 18 * 600 / Decimal(2**32)
    per_ph = mc.hashprice_usd(BTC_PRICE, Decimal("3.125"), difficulty) * Decimal(10) ** 15
    assert Decimal("39") < per_ph < Decimal("40")  # fees, left out, add about 1 %


# --------------------------------------------------------------------------- public inputs

BTC_ANSWERS: dict[str, Any] = {
    "api.coinbase.com": {"data": {"amount": "83400.10", "base": "BTC", "currency": "USD"}},
    "api.kraken.com": {"error": [], "result": {"XXBTZUSD": {"c": ["83479.00000", "0.01"]}}},
    "www.bitstamp.net": {"last": "83500"},
}
DIFFICULTY = "133014000000000.5"


def btc_transport(
    down: tuple[str, ...] = (), prices: dict[str, Any] | None = None
) -> httpx.MockTransport:
    answers = {**BTC_ANSWERS, **(prices or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host in down:
            return httpx.Response(503)
        if host in answers:
            return httpx.Response(200, json=answers[host])
        if host == "mempool.space":
            if path.endswith("/tip/height"):
                return httpx.Response(200, text=str(BTC_HEIGHT))
            return httpx.Response(200, json={"currentDifficulty": float(DIFFICULTY)})
        if host == "blockchain.info":
            return httpx.Response(200, text=DIFFICULTY if "difficulty" in path else str(BTC_HEIGHT))
        if host == "explorer.test":
            if path == "/api/getnetworkhashps":
                return httpx.Response(200, text=str(DMD_HASHRATE))
            if path == "/api/getblockcount":
                return httpx.Response(200, text=str(DMD_HEIGHT))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_btc_price_is_the_median_of_the_exchanges() -> None:
    btc = mc.read_btc_inputs(httpx.Client(transport=btc_transport()))
    assert btc.price == Decimal("83479.00000")
    assert set(btc.prices) == {"coinbase", "kraken", "bitstamp"}
    assert btc.height == BTC_HEIGHT
    assert set(btc.difficulties) == {"mempool.space", "blockchain.info"}


def test_one_exchange_down_is_tolerated_two_are_not() -> None:
    btc = mc.read_btc_inputs(httpx.Client(transport=btc_transport(down=("api.kraken.com",))))
    assert set(btc.prices) == {"coinbase", "bitstamp"}
    down = ("api.kraken.com", "www.bitstamp.net")
    with pytest.raises(mc.InputUnavailable, match="only 1 BTC price"):
        mc.read_btc_inputs(httpx.Client(transport=btc_transport(down=down)))


def test_exchanges_that_disagree_give_no_price() -> None:
    wrong = {"www.bitstamp.net": {"last": "95000"}}
    with pytest.raises(mc.InputUnavailable, match="disagree"):
        mc.read_btc_inputs(httpx.Client(transport=btc_transport(prices=wrong)))


def test_no_difficulty_gives_no_price() -> None:
    down = ("mempool.space", "blockchain.info")
    with pytest.raises(mc.InputUnavailable, match="difficulty"):
        mc.read_btc_inputs(httpx.Client(transport=btc_transport(down=down)))


def test_explorer_gives_the_dmd_hashrate_and_height() -> None:
    explorer = mc.DmdExplorerStats(httpx.Client(transport=btc_transport()), "https://explorer.test")
    dmd = explorer.read()
    assert (dmd.hashrate, dmd.height, dmd.source) == (DMD_HASHRATE, DMD_HEIGHT, "explorer")


class FakeNode:
    def __init__(self, chain: str = "main") -> None:
        self.chain = chain
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: str, params: Any = None) -> Any:
        self.calls.append((method, params))
        return {
            "getblockchaininfo": {"chain": self.chain, "initialblockdownload": False},
            "getnetworkhashps": DMD_HASHRATE,
            "getblockcount": DMD_HEIGHT,
        }[method]


def test_node_averages_the_hashrate_over_a_week() -> None:
    node = FakeNode()
    dmd = mc.DmdNodeStats(node).read()  # type: ignore[arg-type]
    assert ("getnetworkhashps", {"nblocks": 4032}) in node.calls
    assert (dmd.source, dmd.window_blocks) == ("node", 4032)


def test_a_test_chain_node_is_never_used_for_the_mainnet_price() -> None:
    with pytest.raises(mc.InputUnavailable, match="not mainnet"):
        mc.DmdNodeStats(FakeNode("regtest")).read()  # type: ignore[arg-type]


def test_inputs_record_everything_the_price_came_from() -> None:
    btc = mc.read_btc_inputs(httpx.Client(transport=btc_transport()))
    dmd = mc.DmdInputs(DMD_HASHRATE, DMD_HEIGHT, "node", 4032)
    cross = mc.DmdInputs(DMD_HASHRATE * Decimal("1.1"), DMD_HEIGHT, "explorer", None)
    price, inputs = mc.compute_price(btc, dmd, cross)
    assert Decimal("0.00009") < price < Decimal("0.0001")
    assert inputs["dmd_mined_per_day"] == "20160"
    assert inputs["btc_block_subsidy"] == "3.125"
    assert inputs["dmd_hashrate_cross_check"]["difference"] == "0.1000"
    assert inputs["btc_usd_by_source"]["kraken"] == "83479.00000"


def test_refresher_settings() -> None:
    off = Settings(_env_file=None, database_url=UNREACHABLE_DB)
    assert mc.PriceRefresher.from_settings(off, {}) is None
    explorer = Settings(
        _env_file=None, database_url=UNREACHABLE_DB, dmd_price_hashrate_from="explorer"
    )
    assert mc.PriceRefresher.from_settings(explorer, {}) is not None
    node = Settings(_env_file=None, database_url=UNREACHABLE_DB, dmd_price_hashrate_from="node")
    with pytest.raises(Exception, match="missing DMD RPC settings"):
        mc.PriceRefresher.from_settings(node, {})  # the node needs its RPC settings


# --------------------------------------------------------------------------- stored


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()
    command.downgrade(config, "base")


def refresher(
    client: httpx.Client, explorer: Callable[[], mc.DmdInputs] | None = None
) -> mc.PriceRefresher:
    reader = explorer or mc.DmdExplorerStats(client, "https://explorer.test").read
    return mc.PriceRefresher(reader, lambda: mc.read_btc_inputs(client), timedelta(minutes=5))


@needs_db
def test_refresh_stores_the_price_and_quotes_read_it(engine: Engine) -> None:
    factory = session_factory(engine)
    prices = refresher(httpx.Client(transport=btc_transport()))
    now = datetime.now(UTC)
    record = prices.run(factory, now)
    assert record is not None
    assert not prices.due(now + timedelta(minutes=4)) and prices.due(now + timedelta(minutes=5))

    source = mc.StoredMiningCostPrice({})
    source.bind(factory)
    observation = source.get_usd_price("DMD")
    assert observation.usd_per_unit == record.usd_per_unit
    assert observation.source == "dmd_mining_cost"

    # Stored prices are never changed afterwards.
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(text("UPDATE price_records SET usd_per_unit = 1"))


@needs_db
def test_failed_refresh_stores_nothing_and_retries_in_a_minute(engine: Engine) -> None:
    factory = session_factory(engine)
    with engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM price_records")).scalar_one()
    down = btc_transport(down=("api.kraken.com", "www.bitstamp.net"))
    prices = refresher(httpx.Client(transport=down))
    now = datetime.now(UTC)
    assert prices.run(factory, now) is None
    assert prices.due(now + timedelta(seconds=60)) and not prices.due(now + timedelta(seconds=59))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM price_records")).scalar_one() == before


@needs_db
def test_public_endpoint_shows_the_price_and_its_inputs(engine: Engine) -> None:
    settings = Settings(
        _env_file=None, database_url=TEST_DATABASE_URL, route_registry_path=REGISTRY_PATH
    )
    with TestClient(create_app(settings, environ={}, engine=engine)) as client:
        prices = refresher(httpx.Client(transport=btc_transport()))
        assert prices.run(session_factory(engine), datetime.now(UTC)) is not None
    response = client.get("/v1/prices/dmd-usd")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["asset"] == "DMD" and body["network"] == "dmd:mainnet"
    assert Decimal("0.00009") < Decimal(body["usd_per_unit"]) < Decimal("0.0001")
    assert 10_000 < Decimal(body["units_per_usd"]) < 12_000  # DMD per USD
    assert body["fresh"] is True
    assert body["used_for_quotes"] is False  # the DMD rail is off here
    assert body["inputs"]["dmd_hashrate_from"] == "explorer"
    assert response.headers["Cache-Control"] == "public, max-age=60"


def test_no_price_yet_is_a_503(client: TestClient) -> None:
    response = client.get("/v1/prices/dmd-usd")
    assert response.status_code == 503  # here: no database; on a server: not computed yet
