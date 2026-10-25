"""DMD/USD reference price from the cost of mining DMD (DEC-01: the approved interim method,
until DMD has a market price).

DMD is mined with SHA-256, like Bitcoin, so the same machines can mine either. A miner who
points its hashing power at DMD gives up what that power earns on Bitcoin, Bitcoin's
"hashprice". Producing one DMD therefore costs:

    hashprice × DMD network hashrate ÷ DMD mined per day

    hashprice (USD per hash per second, per day)
        = BTC price × BTC block subsidy × 86,400 ÷ (BTC difficulty × 2^32)

Inputs:
  * DMD hashrate: from the gateway's own DMD mainnet node, averaged over a week (4,032
    blocks), so a short burst of rented hashing power barely moves the price. Where there is
    no mainnet node (staging runs a private test chain), from the public DMD explorer.
  * DMD block subsidy: the chain's own rule at the current height (35 DMD, halving every
    300,000 blocks), one block every 150 seconds.
  * BTC price: the median of public exchange prices. At least two must answer, within 2 %
    of each other.
  * BTC difficulty and height: the median of the public sources that answer.
Bitcoin transaction fees (around 1-2 % of miner income) are left out.

``refresh_price`` computes the price (the payment worker runs it every few minutes) and
stores it with all its inputs. Quotes read the latest stored price through
``StoredMiningCostPrice``, so a quote never waits for an outside service, and the price
policy refuses quotes when the stored price is too old.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.adapters.dmd_rpc import DmdRpcClient, RpcError, RpcUnavailable
from dmd_gateway.db.models import PriceRecord
from dmd_gateway.db.session import transaction
from dmd_gateway.pricing.base import PriceObservation, PriceSource, PriceUnavailable
from dmd_gateway.settings import Settings

log = logging.getLogger(__name__)

SOURCE = "dmd_mining_cost"
ASSET = "DMD"
NETWORK = "dmd:mainnet"

# DMD mainnet consensus rules (DebitMyData PoW Core, chainparams and GetBlockSubsidy).
DMD_BLOCK_SECONDS = 150
DMD_INITIAL_SUBSIDY = Decimal(35)
DMD_HALVING_BLOCKS = 300_000
DMD_SUBSIDY_CYCLE_BLOCKS = 9_600_000
DMD_HASHRATE_WINDOW_BLOCKS = 4032  # one week of 150-second blocks

BTC_INITIAL_SUBSIDY = Decimal(50)
BTC_HALVING_BLOCKS = 210_000
TWO_POW_32 = Decimal(2**32)
SECONDS_PER_DAY = 86_400

MIN_BTC_PRICES = 2
MAX_BTC_PRICE_SPREAD = Decimal("0.02")
MAX_BTC_DIFFICULTY_SPREAD = Decimal("0.05")
PRICE_PLACES = Decimal("1e-15")
USER_AGENT = "DebitMyData-Agent-Gateway (price; https://api.debitmydata.io)"


class InputUnavailable(RuntimeError):
    """An input of the price could not be read or failed its checks."""


# --------------------------------------------------------------------------- the formula


def dmd_block_subsidy(height: int) -> Decimal:
    """DMD per block at ``height`` (the chain's GetBlockSubsidy rule)."""
    halvings = (height % DMD_SUBSIDY_CYCLE_BLOCKS) // DMD_HALVING_BLOCKS
    if halvings >= 64:
        return Decimal(0)
    return (DMD_INITIAL_SUBSIDY * 100_000_000 // 2**halvings) / Decimal(100_000_000)


def btc_block_subsidy(height: int) -> Decimal:
    halvings = height // BTC_HALVING_BLOCKS
    if halvings >= 64:
        return Decimal(0)
    return (BTC_INITIAL_SUBSIDY * 100_000_000 // 2**halvings) / Decimal(100_000_000)


def hashprice_usd(btc_price: Decimal, btc_subsidy: Decimal, btc_difficulty: Decimal) -> Decimal:
    """USD earned per day by one hash per second on Bitcoin (subsidy only)."""
    return btc_price * btc_subsidy * SECONDS_PER_DAY / (btc_difficulty * TWO_POW_32)


def dmd_mining_cost_usd(hashprice: Decimal, dmd_hashrate: Decimal, dmd_subsidy: Decimal) -> Decimal:
    """USD cost of mining one DMD: what the network's hashing power would earn on Bitcoin in a
    day, divided by the DMD it mines in a day."""
    dmd_per_day = dmd_subsidy * SECONDS_PER_DAY / DMD_BLOCK_SECONDS
    return hashprice * dmd_hashrate / dmd_per_day


# --------------------------------------------------------------------------- the inputs


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise InputUnavailable(f"not a number: {str(value)[:40]!r}") from exc
    if not number.is_finite() or number <= 0:
        raise InputUnavailable(f"not a positive number: {str(value)[:40]!r}")
    return number


def _json(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json(parse_float=Decimal)


def _text(response: httpx.Response) -> str:
    response.raise_for_status()
    return response.text


# name -> (URL, how to read the BTC/USD price from the answer)
BTC_PRICE_SOURCES: dict[str, tuple[str, Callable[[httpx.Response], Any]]] = {
    "coinbase": (
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        lambda r: _json(r)["data"]["amount"],
    ),
    "kraken": (
        "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
        lambda r: next(iter(_json(r)["result"].values()))["c"][0],
    ),
    "bitstamp": (
        "https://www.bitstamp.net/api/v2/ticker/btcusd/",
        lambda r: _json(r)["last"],
    ),
}
# name -> (difficulty URL and reader, height URL and reader)
BTC_CHAIN_SOURCES: dict[
    str, tuple[str, Callable[[httpx.Response], Any], str, Callable[..., Any]]
] = {
    "mempool.space": (
        "https://mempool.space/api/v1/mining/hashrate/3d",
        lambda r: _json(r)["currentDifficulty"],
        "https://mempool.space/api/blocks/tip/height",
        _text,
    ),
    "blockchain.info": (
        "https://blockchain.info/q/getdifficulty",
        _text,
        "https://blockchain.info/q/getblockcount",
        _text,
    ),
}


@dataclass(frozen=True)
class BtcInputs:
    price: Decimal
    difficulty: Decimal
    height: int
    prices: dict[str, str] = field(default_factory=dict)
    difficulties: dict[str, str] = field(default_factory=dict)


def _spread(values: list[Decimal]) -> Decimal:
    return (max(values) - min(values)) / min(values)


def read_btc_inputs(client: httpx.Client) -> BtcInputs:
    prices: dict[str, Decimal] = {}
    for name, (url, read) in BTC_PRICE_SOURCES.items():
        try:
            prices[name] = _decimal(read(client.get(url)))
        except Exception as exc:  # one source failing is expected now and then
            log.warning(
                "BTC price source failed",
                extra={"event": {"source": name, "error": type(exc).__name__}},
            )
    if len(prices) < MIN_BTC_PRICES:
        raise InputUnavailable(f"only {len(prices)} BTC price source(s) answered")
    if _spread(list(prices.values())) > MAX_BTC_PRICE_SPREAD:
        raise InputUnavailable("BTC price sources disagree by more than 2 %")

    difficulties: dict[str, Decimal] = {}
    heights: list[int] = []
    for name, (diff_url, read_diff, height_url, read_height) in BTC_CHAIN_SOURCES.items():
        try:
            difficulty = _decimal(read_diff(client.get(diff_url)))
            height = int(_decimal(read_height(client.get(height_url))))
        except Exception as exc:
            log.warning(
                "BTC chain source failed",
                extra={"event": {"source": name, "error": type(exc).__name__}},
            )
            continue
        difficulties[name] = difficulty
        heights.append(height)
    if not difficulties:
        raise InputUnavailable("no BTC difficulty source answered")
    if _spread(list(difficulties.values())) > MAX_BTC_DIFFICULTY_SPREAD:
        raise InputUnavailable("BTC difficulty sources disagree by more than 5 %")

    return BtcInputs(
        price=statistics.median(prices.values()),
        difficulty=statistics.median(difficulties.values()),
        height=max(heights),
        prices={name: str(value) for name, value in prices.items()},
        difficulties={name: str(value) for name, value in difficulties.items()},
    )


@dataclass(frozen=True)
class DmdInputs:
    hashrate: Decimal  # hashes per second
    height: int
    source: str
    window_blocks: int | None  # None: the explorer's own averaging window


class DmdNodeStats:
    """DMD hashrate and height from the gateway's own mainnet node (read-only RPC)."""

    def __init__(self, rpc: DmdRpcClient) -> None:
        self._rpc = rpc

    def read(self) -> DmdInputs:
        try:
            chain = self._rpc.call("getblockchaininfo")
            if chain.get("chain") != "main":
                raise InputUnavailable(
                    f"the DMD node is on {chain.get('chain')!r}, not mainnet; "
                    "use the explorer for the hashrate"
                )
            if chain.get("initialblockdownload"):
                raise InputUnavailable("the DMD node is still syncing")
            hashrate = self._rpc.call("getnetworkhashps", {"nblocks": DMD_HASHRATE_WINDOW_BLOCKS})
            height = self._rpc.call("getblockcount")
        except (RpcError, RpcUnavailable) as exc:
            raise InputUnavailable(f"DMD node: {exc}") from exc
        return DmdInputs(_decimal(hashrate), int(height), "node", DMD_HASHRATE_WINDOW_BLOCKS)


class DmdExplorerStats:
    """DMD hashrate and height from the public DMD explorer."""

    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    def read(self) -> DmdInputs:
        try:
            hashrate = _decimal(_text(self._client.get(f"{self._base}/api/getnetworkhashps")))
            height = int(_decimal(_text(self._client.get(f"{self._base}/api/getblockcount"))))
        except (httpx.HTTPError, InputUnavailable) as exc:
            raise InputUnavailable(f"DMD explorer: {type(exc).__name__}") from exc
        return DmdInputs(hashrate, height, "explorer", None)


# --------------------------------------------------------------------------- refresh


def http_client() -> httpx.Client:
    return httpx.Client(timeout=10, follow_redirects=False, headers={"User-Agent": USER_AGENT})


def compute_price(
    btc: BtcInputs, dmd: DmdInputs, cross_check: DmdInputs | None = None
) -> tuple[Decimal, dict[str, Any]]:
    """The DMD/USD price and the record of everything it came from."""
    btc_subsidy = btc_block_subsidy(btc.height)
    dmd_subsidy = dmd_block_subsidy(dmd.height)
    if btc_subsidy <= 0 or dmd_subsidy <= 0:
        raise InputUnavailable("a block subsidy is zero")
    hashprice = hashprice_usd(btc.price, btc_subsidy, btc.difficulty)
    price = dmd_mining_cost_usd(hashprice, dmd.hashrate, dmd_subsidy).quantize(PRICE_PLACES)
    if price <= 0:
        raise InputUnavailable("the DMD hashrate is too low to give a price")
    inputs: dict[str, Any] = {
        "method": "mining_cost",
        "formula": "BTC hashprice × DMD network hashrate ÷ DMD mined per day",
        "btc_usd": str(btc.price),
        "btc_usd_by_source": btc.prices,
        "btc_difficulty": str(btc.difficulty),
        "btc_difficulty_by_source": btc.difficulties,
        "btc_height": btc.height,
        "btc_block_subsidy": str(btc_subsidy),
        "btc_hashprice_usd_per_ph_per_day": str(
            (hashprice * Decimal(10) ** 15).quantize(Decimal("0.01"))
        ),
        "dmd_hashrate": str(dmd.hashrate),
        "dmd_hashrate_from": dmd.source,
        "dmd_hashrate_window_blocks": dmd.window_blocks,
        "dmd_height": dmd.height,
        "dmd_block_subsidy": str(dmd_subsidy),
        "dmd_mined_per_day": str(dmd_subsidy * SECONDS_PER_DAY / DMD_BLOCK_SECONDS),
    }
    if cross_check is not None:
        inputs["dmd_hashrate_cross_check"] = {
            "from": cross_check.source,
            "hashrate": str(cross_check.hashrate),
            "difference": str(
                (abs(cross_check.hashrate - dmd.hashrate) / dmd.hashrate).quantize(
                    Decimal("0.0001")
                )
            ),
        }
    return price, inputs


def refresh_price(
    session: Session,
    dmd: Callable[[], DmdInputs],
    btc: Callable[[], BtcInputs],
    now: datetime,
    *,
    cross_check: Callable[[], DmdInputs] | None = None,
) -> PriceRecord:
    """Compute the price now and store it with its inputs. Raises ``InputUnavailable``."""
    dmd_inputs = dmd()
    checked = None
    if cross_check is not None:
        try:
            checked = cross_check()
        except InputUnavailable as exc:
            log.warning(
                "DMD hashrate cross-check unavailable", extra={"event": {"error": str(exc)}}
            )
    price, inputs = compute_price(btc(), dmd_inputs, checked)
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
        dmd: Callable[[], DmdInputs],
        btc: Callable[[], BtcInputs],
        interval: timedelta,
        cross_check: Callable[[], DmdInputs] | None = None,
    ) -> None:
        self._dmd, self._btc, self._cross_check = dmd, btc, cross_check
        self._interval = interval
        self._next: datetime | None = None

    @classmethod
    def from_settings(cls, settings: Settings, environ: Mapping[str, str]) -> PriceRefresher | None:
        if settings.dmd_price_hashrate_from == "off":
            return None
        client = http_client()
        explorer = DmdExplorerStats(client, settings.dmd_explorer_url)
        interval = timedelta(seconds=settings.dmd_price_refresh_seconds)
        btc = lambda: read_btc_inputs(client)  # noqa: E731
        if settings.dmd_price_hashrate_from == "node":
            node = DmdNodeStats(DmdRpcClient.from_environ(environ))
            return cls(node.read, btc, interval, cross_check=explorer.read)
        return cls(explorer.read, btc, interval)

    def due(self, now: datetime) -> bool:
        return self._next is None or now >= self._next

    def run(self, factory: sessionmaker[Session], now: datetime) -> PriceRecord | None:
        try:
            with transaction(factory) as session:
                record = refresh_price(
                    session, self._dmd, self._btc, now, cross_check=self._cross_check
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
