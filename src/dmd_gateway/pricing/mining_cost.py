"""DMD/USD reference price from the cost of mining DMD (DEC-01: the approved interim method,
until DMD has a market price), never below the minimum rate set by DebitMyData.

Mining one block takes ``difficulty × 2^32`` hashes on average. The electricity those hashes
use is what the block costs to mine; divided by the DMD the block creates, it is the cost of
one DMD:

    USD per DMD = hashes per block × joules per hash × USD per joule ÷ DMD per block

Inputs:
  * hashes per block: from the gateway's own DMD mainnet node, averaged over the last week
    (4,032 blocks) from the chain's total work, so a short burst of extra hashing power
    barely moves it. Where there is no mainnet node (staging runs a private test chain), the
    current difficulty from the public DMD explorer.
  * DMD per block: the chain's own rule at the current height (35 DMD, halving every 300,000
    blocks).
  * the machine efficiency (joules per terahash) and the electricity price (USD per kWh):
    approved values in ``config/payment_routes.yaml`` (``dmd_mining_cost``), reviewed every
    quarter; a review that is overdue is logged and shown with the price.
Nothing comes from another blockchain or from an outside price service. Transaction fees are
left out: they are negligible next to the block reward.

The price is the higher of that mining cost and ``minimum_usd_per_dmd`` (a reference rate
set by DebitMyData while DMD is young and its mining power small) when one is configured.
Both figures are stored with the price, with the one that applied.

``refresh_price`` computes the price (the payment worker runs it every few minutes) and
stores it with all its inputs. Quotes read the latest stored price through
``StoredMiningCostPrice``, so a quote never waits for a node or a website, and the price
policy refuses quotes when the stored price is too old.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.adapters.dmd_rpc import DmdRpcClient, RpcError, RpcUnavailable
from dmd_gateway.db.models import PriceRecord
from dmd_gateway.db.session import transaction
from dmd_gateway.domain.routes import MiningCostParameters, PaymentRouteRegistry
from dmd_gateway.pricing.base import PriceObservation, PriceSource, PriceUnavailable
from dmd_gateway.settings import Settings

log = logging.getLogger(__name__)

SOURCE = "dmd_mining_cost"
ASSET = "DMD"
NETWORK = "dmd:mainnet"
METHOD = "electricity_cost"
METHOD_WITH_MINIMUM = "electricity_cost_with_minimum"
FORMULA = "hashes per block × joules per hash × USD per joule ÷ DMD per block"
PRICE_RULE = "the higher of the minimum rate and the mining cost"

# DMD mainnet consensus rules (DebitMyData PoW Core: chainparams and GetBlockSubsidy).
DMD_BLOCK_SECONDS = 150
DMD_INITIAL_SUBSIDY = Decimal(35)
DMD_HALVING_BLOCKS = 300_000
DMD_SUBSIDY_CYCLE_BLOCKS = 9_600_000
DMD_WINDOW_BLOCKS = 4032  # one week of 150-second blocks
DATA_PER_DMD = 100_000_000  # the smallest unit

TWO_POW_32 = Decimal(2**32)
JOULES_PER_KWH = Decimal(3_600_000)
HASHES_PER_TERAHASH = Decimal(10) ** 12
PRICE_PLACES = Decimal("1e-15")
USER_AGENT = "DebitMyData-Agent-Gateway (price; https://api.debitmydata.io)"


class InputUnavailable(RuntimeError):
    """An input of the price could not be read or failed its checks."""


# --------------------------------------------------------------------------- the chain rules


def dmd_block_subsidy(height: int) -> Decimal:
    """DMD created by the block at ``height`` (the chain's GetBlockSubsidy rule)."""
    halvings = (height % DMD_SUBSIDY_CYCLE_BLOCKS) // DMD_HALVING_BLOCKS
    if halvings >= 64:
        return Decimal(0)
    data = (int(DMD_INITIAL_SUBSIDY) * DATA_PER_DMD) >> halvings
    return Decimal(data) / DATA_PER_DMD


def dmd_coins_mined(height: int) -> Decimal:
    """DMD created by blocks 1 to ``height`` (the genesis block's coins cannot be spent)."""
    total = 0
    start = 1
    while start <= height:
        era_end = min(height, (start // DMD_HALVING_BLOCKS + 1) * DMD_HALVING_BLOCKS - 1)
        subsidy = int(dmd_block_subsidy(start) * DATA_PER_DMD)
        total += subsidy * (era_end - start + 1)
        start = era_end + 1
    return Decimal(total) / DATA_PER_DMD


# --------------------------------------------------------------------------- the formula


def dmd_mining_cost_usd(
    hashes_per_block: Decimal, dmd_per_block: Decimal, parameters: MiningCostParameters
) -> Decimal:
    """USD of electricity it takes to mine one DMD."""
    joules = hashes_per_block / HASHES_PER_TERAHASH * parameters.efficiency_joules_per_terahash
    usd_per_block = joules / JOULES_PER_KWH * parameters.electricity_usd_per_kwh
    return usd_per_block / dmd_per_block


def review_due_on(parameters: MiningCostParameters) -> date:
    return parameters.reviewed_on + timedelta(days=parameters.review_every_days)


# --------------------------------------------------------------------------- chain figures


@dataclass(frozen=True)
class ChainFigures:
    hashes_per_block: Decimal
    difficulty: Decimal
    height: int
    source: str  # "node" or "explorer"
    window_blocks: int | None  # blocks averaged; None: the current difficulty only


def _positive(value: Any) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise InputUnavailable(f"not a number: {str(value)[:40]!r}") from exc
    if not number.is_finite() or number <= 0:
        raise InputUnavailable(f"not a positive number: {str(value)[:40]!r}")
    return number


class DmdNodeFigures:
    """Work per block over the last week, from the gateway's own mainnet node (read-only)."""

    def __init__(self, rpc: DmdRpcClient) -> None:
        self._rpc = rpc

    def read(self) -> ChainFigures:
        try:
            chain = self._rpc.call("getblockchaininfo")
            if chain.get("chain") != "main":
                raise InputUnavailable(
                    f"the DebitMyData node is on {chain.get('chain')!r}, not mainnet; "
                    "use the explorer for the chain figures"
                )
            if chain.get("initialblockdownload"):
                raise InputUnavailable("the DebitMyData node is still syncing")
            tip = int(chain["blocks"])
            window = min(DMD_WINDOW_BLOCKS, tip)
            if window < 1:
                raise InputUnavailable("the DebitMyData chain has no blocks yet")
            start_hash = self._rpc.call("getblockhash", {"height": tip - window})
            tip_header = self._rpc.call("getblockheader", {"blockhash": chain["bestblockhash"]})
            start_header = self._rpc.call("getblockheader", {"blockhash": start_hash})
        except (RpcError, RpcUnavailable, KeyError, TypeError, ValueError) as exc:
            raise InputUnavailable(f"DebitMyData node: {exc}") from exc
        if int(tip_header.get("height", -1)) != tip:
            raise InputUnavailable("the DebitMyData node moved to a new block while being read")
        work = int(tip_header["chainwork"], 16) - int(start_header["chainwork"], 16)
        return ChainFigures(
            hashes_per_block=_positive(Decimal(work) / window),
            difficulty=_positive(chain.get("difficulty")),
            height=tip,
            source="node",
            window_blocks=window,
        )


class DmdExplorerFigures:
    """The current difficulty and height from the public DebitMyData explorer."""

    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    def _get(self, path: str) -> Decimal:
        response = self._client.get(f"{self._base}{path}")
        response.raise_for_status()
        return _positive(response.text)

    def read(self) -> ChainFigures:
        try:
            difficulty = self._get("/api/getdifficulty")
            height = int(self._get("/api/getblockcount"))
        except (httpx.HTTPError, InputUnavailable) as exc:
            raise InputUnavailable(f"DebitMyData explorer: {type(exc).__name__}") from exc
        return ChainFigures(difficulty * TWO_POW_32, difficulty, height, "explorer", None)


def http_client() -> httpx.Client:
    return httpx.Client(timeout=10, follow_redirects=False, headers={"User-Agent": USER_AGENT})


# --------------------------------------------------------------------------- refresh


def compute_price(
    chain: ChainFigures,
    parameters: MiningCostParameters,
    cross_check: ChainFigures | None = None,
) -> tuple[Decimal, dict[str, Any]]:
    """The DMD/USD price and the record of everything it came from."""
    dmd_per_block = dmd_block_subsidy(chain.height)
    if dmd_per_block <= 0:
        raise InputUnavailable("the DebitMyData block reward is zero")
    cost = dmd_mining_cost_usd(chain.hashes_per_block, dmd_per_block, parameters)
    cost = cost.quantize(PRICE_PLACES)
    minimum = parameters.minimum_usd_per_dmd
    if minimum is None and cost <= 0:
        raise InputUnavailable("the DebitMyData difficulty is too low to give a price")
    from_minimum = minimum is not None and minimum > cost
    price = minimum if from_minimum and minimum is not None else cost
    kwh_per_block = (
        chain.hashes_per_block
        / HASHES_PER_TERAHASH
        * parameters.efficiency_joules_per_terahash
        / JOULES_PER_KWH
    )
    inputs: dict[str, Any] = {
        "method": METHOD if minimum is None else METHOD_WITH_MINIMUM,
        "formula": FORMULA,
        "mining_cost_usd_per_dmd": format(cost.normalize(), "f"),
        "chain_data_from": chain.source,
        "averaged_over_blocks": chain.window_blocks,
        "dmd_height": chain.height,
        "dmd_difficulty": str(chain.difficulty),
        "dmd_hashes_per_block": str(chain.hashes_per_block.quantize(Decimal(1))),
        "dmd_network_hashrate": str(
            (chain.hashes_per_block / DMD_BLOCK_SECONDS).quantize(Decimal(1))
        ),
        "dmd_per_block": str(dmd_per_block),
        "dmd_coins_mined": str(dmd_coins_mined(chain.height)),
        "efficiency_joules_per_terahash": str(parameters.efficiency_joules_per_terahash),
        "electricity_usd_per_kwh": str(parameters.electricity_usd_per_kwh),
        "kwh_per_block": str(kwh_per_block.quantize(Decimal("0.000001"))),
        "usd_per_block": str(
            (kwh_per_block * parameters.electricity_usd_per_kwh).quantize(Decimal("0.00000001"))
        ),
        "parameters_reviewed_on": parameters.reviewed_on.isoformat(),
        "parameters_review_due_on": review_due_on(parameters).isoformat(),
    }
    if minimum is not None:
        inputs["price_rule"] = PRICE_RULE
        inputs["minimum_usd_per_dmd"] = format(minimum.normalize(), "f")
        inputs["minimum_approved_on"] = (
            parameters.minimum_approved_on.isoformat() if parameters.minimum_approved_on else None
        )
        inputs["price_from"] = "minimum" if from_minimum else "mining_cost"
    if cross_check is not None:
        difference = abs(cross_check.hashes_per_block - chain.hashes_per_block)
        inputs["cross_check"] = {
            "from": cross_check.source,
            "dmd_difficulty": str(cross_check.difficulty),
            "difference": str((difference / chain.hashes_per_block).quantize(Decimal("0.0001"))),
        }
    return price, inputs


def refresh_price(
    session: Session,
    chain: Callable[[], ChainFigures],
    parameters: MiningCostParameters,
    now: datetime,
    *,
    cross_check: Callable[[], ChainFigures] | None = None,
) -> PriceRecord:
    """Compute the price now and store it with its inputs. Raises ``InputUnavailable``."""
    figures = chain()
    checked = None
    if cross_check is not None:
        try:
            checked = cross_check()
        except InputUnavailable as exc:
            log.warning("DMD price cross-check unavailable", extra={"event": {"error": str(exc)}})
    price, inputs = compute_price(figures, parameters, checked)
    record = PriceRecord(
        source=SOURCE,
        asset_symbol=ASSET,
        network=NETWORK,
        usd_per_unit=price,
        observed_at=now,
        inputs=inputs,
    )
    session.add(record)
    session.flush()
    return record


RETRY_AFTER_FAILURE = timedelta(seconds=60)


class PriceRefresher:
    """Computes and stores the price every ``interval``; run by the payment worker."""

    def __init__(
        self,
        chain: Callable[[], ChainFigures],
        parameters: MiningCostParameters,
        interval: timedelta,
        cross_check: Callable[[], ChainFigures] | None = None,
    ) -> None:
        self._chain, self._parameters, self._cross_check = chain, parameters, cross_check
        self._interval = interval
        self._next: datetime | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        environ: Mapping[str, str],
        registry: PaymentRouteRegistry,
    ) -> PriceRefresher | None:
        if settings.dmd_price_chain_data_from == "off":
            return None
        parameters = registry.dmd_mining_cost
        if parameters is None:
            log.error("no DMD price: dmd_mining_cost is not set in the payment route registry")
            return None
        explorer = DmdExplorerFigures(http_client(), settings.dmd_explorer_url)
        interval = timedelta(seconds=settings.dmd_price_refresh_seconds)
        if settings.dmd_price_chain_data_from == "node":
            node = DmdNodeFigures(DmdRpcClient.from_environ(environ))
            return cls(node.read, parameters, interval, cross_check=explorer.read)
        return cls(explorer.read, parameters, interval)

    def due(self, now: datetime) -> bool:
        return self._next is None or now >= self._next

    def run(self, factory: sessionmaker[Session], now: datetime) -> PriceRecord | None:
        try:
            with transaction(factory) as session:
                record = refresh_price(
                    session, self._chain, self._parameters, now, cross_check=self._cross_check
                )
        except InputUnavailable as exc:
            self._next = now + RETRY_AFTER_FAILURE
            log.warning("DMD price not refreshed", extra={"event": {"error": str(exc)}})
            return None
        self._next = now + self._interval
        log.info(
            "DMD price refreshed",
            extra={"event": {"usd_per_dmd": str(record.usd_per_unit), "inputs": record.inputs}},
        )
        if now.date() > review_due_on(self._parameters):
            log.warning(
                "DMD price parameters are due for review",
                extra={"event": {"reviewed_on": self._parameters.reviewed_on.isoformat()}},
            )
        return record


def latest_record(session: Session) -> PriceRecord | None:
    return session.scalars(
        select(PriceRecord)
        .where(PriceRecord.source == SOURCE, PriceRecord.asset_symbol == ASSET)
        .order_by(PriceRecord.observed_at.desc())
        .limit(1)
    ).first()


class StoredMiningCostPrice(PriceSource):
    """The latest stored mining-cost price, for quotes (age is checked by the price policy)."""

    name = SOURCE

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._factory: sessionmaker[Session] | None = None

    def bind(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def get_usd_price(self, asset_symbol: str) -> PriceObservation:
        if asset_symbol != ASSET:
            raise PriceUnavailable(f"{SOURCE} only prices {ASSET}")
        if self._factory is None:
            raise PriceUnavailable(f"{SOURCE} has no database")
        with self._factory() as session:
            record = latest_record(session)
        if record is None:
            raise PriceUnavailable("no DMD mining-cost price has been computed yet")
        return PriceObservation(
            source=SOURCE,
            asset_symbol=ASSET,
            usd_per_unit=record.usd_per_unit,
            observed_at=record.observed_at,
        )
