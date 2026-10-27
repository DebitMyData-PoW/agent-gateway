"""The DMD/USD mining-cost price: the chain rules, the formula, the chain figures from the
node or the explorer, storage, the worker's refresher and the public endpoint.

The formula and the chain figures need no database; storing, quoting and the endpoint use
the disposable PostgreSQL database named by TEST_DATABASE_URL and are skipped without it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alembic import command
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from dmd_gateway.db.session import session_factory
from dmd_gateway.domain.money import usd_to_base_units
from dmd_gateway.domain.routes import MiningCostParameters, RegistryError, load_registry
from dmd_gateway.main import create_app
from dmd_gateway.pricing import mining_cost as mc
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL, UNREACHABLE_DB, alembic_config

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
REGTEST_PATH = REGISTRY_PATH.with_name("payment_routes.regtest.yaml")

# The explorer's figures on October 24, 2026.
DIFFICULTY = "1652896.002453423"
HEIGHT = 135_879
COINS_MINED = Decimal(4_755_765)
PARAMETERS = MiningCostParameters(
    electricity_usd_per_kwh="0.06",  # type: ignore[arg-type]
    efficiency_joules_per_terahash="20",  # type: ignore[arg-type]
    reviewed_on=date(2026, 10, 24),
)
WITH_MINIMUM = PARAMETERS.model_copy(update={"minimum_usd_per_dmd": Decimal("0.001")})


# --------------------------------------------------------------------------- chain rules


@pytest.mark.parametrize(
    ("height", "subsidy"),
    [
        (1, "35"),
        (HEIGHT, "35"),
        (299_999, "35"),
        (300_000, "17.5"),
        (600_000, "8.75"),
        (9_599_999, "0.00000001"),  # 31 halvings: one data (the smallest unit)
        (9_600_000, "35"),  # the halvings start again every 9,600,000 blocks
    ],
)
def test_dmd_reward_follows_the_chain_rule(height: int, subsidy: str) -> None:
    assert mc.dmd_block_subsidy(height) == Decimal(subsidy)


def test_coins_mined_match_the_explorer() -> None:
    assert mc.dmd_coins_mined(HEIGHT) == COINS_MINED
    assert mc.dmd_coins_mined(300_000) == Decimal(299_999 * 35) + Decimal("17.5")
    assert mc.dmd_coins_mined(0) == 0


# --------------------------------------------------------------------------- the formula


def test_todays_figures() -> None:
    hashes = Decimal(DIFFICULTY) * 2**32
    price = mc.dmd_mining_cost_usd(hashes, Decimal(35), PARAMETERS)
    # 7,099 TH per block × 20 J/TH = 0.0394 kWh, at 0.06 USD = 0.00237 USD per 35 DMD.
    assert Decimal("0.0000675") < price < Decimal("0.0000677")
    assert 369_000 < Decimal(25) / price < 371_000  # DMD for a $25 DID


def test_the_price_follows_the_electricity_price_and_the_work() -> None:
    hashes = Decimal(DIFFICULTY) * 2**32
    base = mc.dmd_mining_cost_usd(hashes, Decimal(35), PARAMETERS)
    dearer = PARAMETERS.model_copy(update={"electricity_usd_per_kwh": Decimal("0.12")})
    assert mc.dmd_mining_cost_usd(hashes, Decimal(35), dearer) == base * 2
    assert mc.dmd_mining_cost_usd(hashes * 10, Decimal(35), PARAMETERS) == base * 10
    assert mc.dmd_mining_cost_usd(hashes, Decimal("17.5"), PARAMETERS) == base * 2


def test_parameters_must_be_written_exactly() -> None:
    with pytest.raises(ValidationError, match="quoted decimal string"):
        MiningCostParameters(
            electricity_usd_per_kwh=0.06,  # type: ignore[arg-type]
            efficiency_joules_per_terahash="20",  # type: ignore[arg-type]
            reviewed_on=date(2026, 10, 24),
        )
    with pytest.raises(ValidationError, match="greater than zero"):
        MiningCostParameters(
            electricity_usd_per_kwh="0",  # type: ignore[arg-type]
            efficiency_joules_per_terahash="20",  # type: ignore[arg-type]
            reviewed_on=date(2026, 10, 24),
        )
    base = {
        "electricity_usd_per_kwh": "0.06",
        "efficiency_joules_per_terahash": "20",
        "reviewed_on": date(2026, 10, 24),
    }
    for minimum, message in ((0.001, "quoted decimal string"), ("0", "greater than zero")):
        with pytest.raises(ValidationError, match=message):
            MiningCostParameters(**base, minimum_usd_per_dmd=minimum)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="minimum_usd_per_dmd is not"):
        MiningCostParameters(**base, minimum_approved_on=date(2026, 10, 26))  # type: ignore[arg-type]
    approved = MiningCostParameters(
        **base,  # type: ignore[arg-type]
        minimum_usd_per_dmd="0.001",  # type: ignore[arg-type]
        minimum_approved_on=date(2026, 10, 26),
    )
    assert approved.minimum_usd_per_dmd == Decimal("0.001")


@pytest.mark.parametrize("path", [REGISTRY_PATH, REGTEST_PATH])
def test_shipped_registries_carry_the_approved_parameters(path: Path) -> None:
    registry = load_registry(path, environ={}, available_adapters=set())
    assert registry.dmd_mining_cost is not None
    assert registry.dmd_mining_cost.electricity_usd_per_kwh == Decimal("0.06")
    assert registry.dmd_mining_cost.efficiency_joules_per_terahash == Decimal(20)
    # Proposed in October 2026 and approved by DebitMyData on 27 October 2026.
    assert registry.dmd_mining_cost.minimum_usd_per_dmd == Decimal("0.001")
    assert registry.dmd_mining_cost.minimum_approved_on == date(2026, 10, 27)


def test_a_rail_priced_by_mining_cost_needs_the_parameters(tmp_path: Path) -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    del data["dmd_mining_cost"]
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RegistryError, match="dmd_mining_cost parameters are not set"):
        load_registry(
            path,
            environ={"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"},
            available_adapters={"DMDNativePaymentAdapter"},
            available_price_sources={"dmd_mining_cost"},
        )


# --------------------------------------------------------------------------- chain figures


def explorer_transport(down: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            return httpx.Response(503)
        answers = {"/api/getdifficulty": DIFFICULTY, "/api/getblockcount": str(HEIGHT)}
        if request.url.path in answers:
            return httpx.Response(200, text=answers[request.url.path])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def explorer(down: bool = False) -> mc.DmdExplorerFigures:
    return mc.DmdExplorerFigures(httpx.Client(transport=explorer_transport(down)), "https://x.test")


def test_explorer_gives_the_current_difficulty_and_height() -> None:
    figures = explorer().read()
    assert figures.difficulty == Decimal(DIFFICULTY)
    assert figures.hashes_per_block == Decimal(DIFFICULTY) * 2**32
    assert (figures.height, figures.source, figures.window_blocks) == (HEIGHT, "explorer", None)
    with pytest.raises(mc.InputUnavailable, match="explorer"):
        explorer(down=True).read()


class FakeNode:
    """A mainnet node whose last 4,032 blocks each took 7e15 hashes on average."""

    WORK_PER_BLOCK = 7 * 10**15

    def __init__(self, chain: str = "main", moved: bool = False) -> None:
        self.chain, self.moved = chain, moved
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: str, params: Any = None) -> Any:
        self.calls.append((method, params))
        if method == "getblockchaininfo":
            return {
                "chain": self.chain,
                "initialblockdownload": False,
                "blocks": HEIGHT,
                "bestblockhash": "tip",
                "difficulty": Decimal(DIFFICULTY),
            }
        if method == "getblockhash":
            return "start"
        if method == "getblockheader":
            if params["blockhash"] == "tip":
                work = (HEIGHT + 1) * self.WORK_PER_BLOCK
                return {"height": HEIGHT + int(self.moved), "chainwork": f"{work:064x}"}
            work = (HEIGHT + 1 - 4032) * self.WORK_PER_BLOCK
            return {"height": HEIGHT - 4032, "chainwork": f"{work:064x}"}
        raise AssertionError(method)


def test_node_averages_the_work_of_the_last_week() -> None:
    node = FakeNode()
    figures = mc.DmdNodeFigures(node).read()  # type: ignore[arg-type]
    assert ("getblockhash", {"height": HEIGHT - 4032}) in node.calls
    assert figures.hashes_per_block == FakeNode.WORK_PER_BLOCK
    assert (figures.source, figures.window_blocks, figures.height) == ("node", 4032, HEIGHT)


def test_a_test_chain_node_is_never_used_for_the_mainnet_price() -> None:
    with pytest.raises(mc.InputUnavailable, match="not mainnet"):
        mc.DmdNodeFigures(FakeNode("regtest")).read()  # type: ignore[arg-type]


def test_a_node_moving_to_a_new_block_is_read_again_later() -> None:
    with pytest.raises(mc.InputUnavailable, match="new block"):
        mc.DmdNodeFigures(FakeNode(moved=True)).read()  # type: ignore[arg-type]


def test_inputs_record_everything_the_price_came_from() -> None:
    node = mc.DmdNodeFigures(FakeNode()).read()  # type: ignore[arg-type]
    price, inputs = mc.compute_price(node, PARAMETERS, explorer().read())
    exact = mc.dmd_mining_cost_usd(node.hashes_per_block, Decimal(35), PARAMETERS)
    assert price == exact.quantize(Decimal("1e-15"))
    assert inputs["method"] == "electricity_cost"
    assert inputs["chain_data_from"] == "node" and inputs["averaged_over_blocks"] == 4032
    assert inputs["dmd_per_block"] == "35"
    assert inputs["dmd_coins_mined"] == str(COINS_MINED)
    assert inputs["electricity_usd_per_kwh"] == "0.06"
    assert inputs["efficiency_joules_per_terahash"] == "20"
    assert inputs["kwh_per_block"] == "0.038889"  # 7,000 TH × 20 J/TH
    assert inputs["parameters_review_due_on"] == "2027-01-24"
    assert inputs["cross_check"]["from"] == "explorer"
    assert "btc" not in str(inputs).lower()  # nothing from another blockchain


# --------------------------------------------------------------------------- the minimum rate


def test_the_minimum_applies_while_the_mining_cost_is_below_it() -> None:
    price, inputs = mc.compute_price(explorer().read(), WITH_MINIMUM)
    assert price == Decimal("0.001")
    assert inputs["method"] == "electricity_cost_with_minimum"
    assert inputs["price_from"] == "minimum"
    assert inputs["minimum_usd_per_dmd"] == "0.001"
    assert inputs["minimum_approved_on"] is None
    assert inputs["price_rule"] == "the higher of the minimum rate and the mining cost"
    # The mining cost is still computed and recorded next to it.
    assert Decimal("0.0000675") < Decimal(inputs["mining_cost_usd_per_dmd"]) < Decimal("0.0000677")
    # A 25.00 USD DID: 25,000 DMD (in data, the smallest unit).
    assert usd_to_base_units("25.00", price, 8) == 25_000 * 10**8


def test_the_mining_cost_applies_once_it_is_above_the_minimum() -> None:
    # About 15 times today's work per block makes mining 1 DMD cost more than 0.001 USD.
    busy = mc.ChainFigures(
        Decimal(DIFFICULTY) * 2**32 * 16, Decimal(DIFFICULTY) * 16, HEIGHT, "node", 4032
    )
    price, inputs = mc.compute_price(busy, WITH_MINIMUM)
    assert price > Decimal("0.001")
    assert inputs["price_from"] == "mining_cost"
    assert Decimal(inputs["mining_cost_usd_per_dmd"]) == price


def test_a_one_hour_burst_of_hashing_power_does_not_move_the_price() -> None:
    """The node averages a week of work: 24 blocks (one hour) mined at 30 times the usual
    difficulty raise the weekly figure by about 17%, far from the 15 times needed to pass
    the minimum."""
    usual = Decimal(DIFFICULTY) * 2**32
    burst = (usual * (4032 - 24) + usual * 30 * 24) / 4032
    figures = mc.ChainFigures(burst, Decimal(DIFFICULTY), HEIGHT, "node", 4032)
    price, inputs = mc.compute_price(figures, WITH_MINIMUM)
    assert price == Decimal("0.001") and inputs["price_from"] == "minimum"


def test_approval_date_is_recorded_with_the_price() -> None:
    approved = WITH_MINIMUM.model_copy(update={"minimum_approved_on": date(2026, 10, 27)})
    _, inputs = mc.compute_price(explorer().read(), approved)
    assert inputs["minimum_approved_on"] == "2026-10-27"


def test_refresher_settings() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())

    def settings(mode: str) -> Settings:
        return Settings(_env_file=None, database_url=UNREACHABLE_DB, dmd_price_chain_data_from=mode)

    assert mc.PriceRefresher.from_settings(settings("off"), {}, registry) is None
    assert mc.PriceRefresher.from_settings(settings("explorer"), {}, registry) is not None
    with pytest.raises(Exception, match="missing DebitMyData node RPC settings"):
        mc.PriceRefresher.from_settings(settings("node"), {}, registry)  # needs the node's RPC


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
    down: bool = False, parameters: MiningCostParameters = PARAMETERS
) -> mc.PriceRefresher:
    return mc.PriceRefresher(explorer(down).read, parameters, timedelta(minutes=5))


@needs_db
def test_refresh_stores_the_price_and_quotes_read_it(engine: Engine) -> None:
    factory = session_factory(engine)
    prices = refresher()
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
    prices = refresher(down=True)
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
        assert refresher().run(session_factory(engine), datetime.now(UTC)) is not None
        response = client.get("/v1/prices/dmd-usd")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["asset"] == "DMD" and body["network"] == "dmd:mainnet"
        assert body["note"].startswith("DMD is the ticker of the DebitMyData coin, the native coin")
        assert Decimal("0.0000675") < Decimal(body["usd_per_unit"]) < Decimal("0.0000677")
        assert 14_000 < Decimal(body["units_per_usd"]) < 15_000  # DMD per USD
        assert body["fresh"] is True
        assert body["used_for_quotes"] is False  # the DMD rail is off here
        assert body["parameters_review_overdue"] is False
        assert body["inputs"]["chain_data_from"] == "explorer"
        assert response.headers["Cache-Control"] == "public, max-age=60"

        assert body["price_from"] == "mining_cost"
        assert body["mining_cost_usd_per_unit"] == body["usd_per_unit"]
        assert body["minimum_usd_per_unit"] is None
        assert "minimum" not in body["note"]

        stale = PARAMETERS.model_copy(update={"reviewed_on": date(2026, 1, 1)})
        assert refresher(parameters=stale).run(session_factory(engine), datetime.now(UTC))
        assert client.get("/v1/prices/dmd-usd").json()["parameters_review_overdue"] is True


@needs_db
def test_public_endpoint_shows_the_minimum_and_the_mining_cost(engine: Engine) -> None:
    settings = Settings(
        _env_file=None, database_url=TEST_DATABASE_URL, route_registry_path=REGISTRY_PATH
    )
    approved = WITH_MINIMUM.model_copy(update={"minimum_approved_on": date(2026, 10, 27)})
    with TestClient(create_app(settings, environ={}, engine=engine)) as client:
        assert refresher(parameters=approved).run(session_factory(engine), datetime.now(UTC))
        body = client.get("/v1/prices/dmd-usd").json()
    assert body["usd_per_unit"] == "0.001"
    assert body["units_per_usd"] == "1000.00000000"  # DMD per USD
    assert body["method"] == "electricity_cost_with_minimum"
    assert body["price_from"] == "minimum"
    assert body["minimum_usd_per_unit"] == "0.001"
    assert body["minimum_approved_on"] == "2026-10-27"
    assert Decimal("0.0000675") < Decimal(body["mining_cost_usd_per_unit"]) < Decimal("0.0000677")
    assert "minimum rate" in body["note"] and "not a market price" in body["note"]


def test_no_price_yet_is_a_503(client: TestClient) -> None:
    response = client.get("/v1/prices/dmd-usd")
    assert response.status_code == 503  # here: no database; on a server: not computed yet
