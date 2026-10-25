"""Public DMD/USD reference price, with every input it was computed from."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from dmd_gateway.db.session import transaction
from dmd_gateway.domain.routes import RailState
from dmd_gateway.errors import ApiError
from dmd_gateway.pricing.mining_cost import ASSET, NETWORK, SOURCE, latest_record

router = APIRouter(prefix="/v1", tags=["discovery"])

DMD_RAIL = "dmd_pow_native"
DEFAULT_MAX_AGE_SECONDS = 900
NOTE = (
    "Reference price from the cost of mining DMD: Bitcoin's hashprice (what SHA-256 mining "
    "earns on Bitcoin per day) times the DMD network hashrate, divided by the DMD mined per "
    "day. It is not a market price."
)


class DmdPrice(BaseModel):
    asset: str
    network: str
    usd_per_unit: str
    units_per_usd: str
    method: str
    observed_at: datetime
    age_seconds: int
    fresh: bool = Field(description="Recent enough to be used in a quote.")
    used_for_quotes: bool = Field(description="Whether DMD quotes on this server use it.")
    note: str
    inputs: dict[str, Any]


@router.get(
    "/prices/dmd-usd",
    response_model=DmdPrice,
    summary="DMD/USD reference price and how it was computed",
    responses={503: {"description": "No price has been computed yet."}},
)
def dmd_price(request: Request, response: Response) -> DmdPrice:
    with transaction(request.app.state.session_factory) as session:
        record = latest_record(session)
        if record is None:
            raise ApiError(
                503,
                "price_unavailable",
                "No DMD price has been computed yet; try again in a few minutes.",
                headers={"Retry-After": "300"},
            )
        price, observed_at, inputs = record.usd_per_unit, record.observed_at, dict(record.inputs)

    registry = request.app.state.registry
    rail = registry.rails.get(DMD_RAIL)
    max_age = DEFAULT_MAX_AGE_SECONDS
    used = False
    if rail is not None:
        max_age = rail.price_policy.max_price_age_seconds or DEFAULT_MAX_AGE_SECONDS
        used = (
            registry.rail_state(DMD_RAIL) is RailState.ENABLED
            and SOURCE in (rail.price_policy.source, rail.price_policy.secondary_source)
            and rail.network == NETWORK
        )
    age = max(0, int((datetime.now(UTC) - observed_at).total_seconds()))
    response.headers["Cache-Control"] = "public, max-age=60"
    return DmdPrice(
        asset=ASSET,
        network=NETWORK,
        usd_per_unit=format(price.normalize(), "f"),
        units_per_usd=format((Decimal(1) / price).quantize(Decimal("0.00000001")), "f"),
        method=str(inputs.get("method", "mining_cost")),
        observed_at=observed_at,
        age_seconds=age,
        fresh=age <= max_age,
        used_for_quotes=used,
        note=NOTE,
        inputs=inputs,
    )
