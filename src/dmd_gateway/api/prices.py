"""Public DMD/USD reference price, with every input it was computed from.

DMD is the ticker of the DebitMyData coin, the native coin of the DebitMyData Proof-of-Work
chain.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
ABOUT_DMD = (
    "DMD is the ticker of the DebitMyData coin, the native coin of the DebitMyData "
    "Proof-of-Work chain (addresses start with dm1); it is unrelated to other tokens that "
    "use the same ticker. "
)
NOTE = ABOUT_DMD + (
    "Reference price from the cost of mining DMD: the electricity it takes to mine a block "
    "(the block's work at the approved machine efficiency and electricity price) divided by "
    "the DMD the block creates. It is not a market price."
)
NOTE_WITH_MINIMUM = ABOUT_DMD + (
    "Reference price for DebitMyData Gateway payments until DMD has a market price: the "
    "higher of DebitMyData's minimum rate and the cost of mining DMD (the electricity it "
    "takes to mine a block, at the approved machine efficiency and electricity price, divided "
    "by the DMD the block creates). It is not a market price."
)


class DmdPrice(BaseModel):
    asset: str
    network: str
    usd_per_unit: str
    units_per_usd: str
    method: str
    price_from: str = Field(
        description="mining_cost, or minimum when the minimum rate is higher than the mining cost."
    )
    mining_cost_usd_per_unit: str = Field(
        description="What it costs to mine 1 DMD (DebitMyData coin)."
    )
    minimum_usd_per_unit: str | None = Field(
        description="DebitMyData's minimum rate until DMD has a market price; null if none."
    )
    minimum_approved_on: date | None = None
    observed_at: datetime
    age_seconds: int
    fresh: bool = Field(description="Recent enough to be used in a quote.")
    used_for_quotes: bool = Field(description="Whether DMD quotes on this server use it.")
    parameters_review_overdue: bool = Field(
        description="The approved electricity price and machine efficiency are past their "
        "quarterly review."
    )
    note: str
    inputs: dict[str, Any]


@router.get(
    "/prices/dmd-usd",
    response_model=DmdPrice,
    summary="DMD/USD reference price (DMD: the DebitMyData coin) and how it was computed",
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
        price_from=str(inputs.get("price_from", "mining_cost")),
        # Prices stored before the minimum rate existed are the mining cost itself.
        mining_cost_usd_per_unit=str(
            inputs.get("mining_cost_usd_per_dmd") or format(price.normalize(), "f")
        ),
        minimum_usd_per_unit=inputs.get("minimum_usd_per_dmd"),
        minimum_approved_on=_date(inputs.get("minimum_approved_on")),
        observed_at=observed_at,
        age_seconds=age,
        fresh=age <= max_age,
        used_for_quotes=used,
        parameters_review_overdue=_overdue(inputs),
        note=NOTE_WITH_MINIMUM if inputs.get("minimum_usd_per_dmd") else NOTE,
        inputs=inputs,
    )


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _overdue(inputs: dict[str, Any]) -> bool:
    due = inputs.get("parameters_review_due_on")
    try:
        return due is not None and datetime.now(UTC).date() > date.fromisoformat(str(due))
    except ValueError:
        return False
