"""Public discovery endpoints (DOC-04, DOC-05).

These describe only what is actually live: a payment option appears only when its rail is
enabled, and the MCP endpoint appears only once it is configured.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from dmd_gateway import __version__
from dmd_gateway.domain.routes import PaymentRouteRegistry, RailState

router = APIRouter(prefix="/v1", tags=["discovery"])

# Wording as specified in the directive's payment-options response.
AVAILABILITY_NOTICE = (
    "Payment availability may vary by asset, network, wallet policy, geographic eligibility, "
    "and DebitMyData risk controls."
)


class Money(BaseModel):
    currency: str
    amount: str


class Offer(BaseModel):
    id: str
    title: str
    description: str
    method: str
    path: str
    resource: str
    commercial_price: Money
    quote_required: bool
    accepting_payments: bool
    payment_options_url: str


class Discovery(BaseModel):
    service: str
    version: str
    offers: list[Offer]
    mcp_url: str | None
    policy_urls: dict[str, str]
    keywords: list[str]


class PaymentOption(BaseModel):
    id: str
    name: str
    asset: str
    network: str
    payment_protocol: str
    scheme: str | None
    wallet_compatibility: list[str]
    status: RailState


class PaymentOptions(BaseModel):
    service: str
    commercial_fee: Money
    supported_payment_options: list[PaymentOption]
    notice: str


def _registry(request: Request) -> PaymentRouteRegistry:
    return request.app.state.registry


@router.get("/discovery", response_model=Discovery, summary="Describe DebitMyData services")
def discovery(request: Request) -> Discovery:
    registry = _registry(request)
    settings = request.app.state.settings
    base = settings.public_base_url.rstrip("/")
    offers = []
    policy_urls: dict[str, str] = {}
    for service_id, service in registry.services.items():
        offers.append(
            Offer(
                id=service_id,
                title=service.title,
                description=service.description,
                method=service.method,
                path=service.path,
                resource=f"{base}{service.path}",
                commercial_price=Money(
                    currency=service.commercial_price.currency,
                    amount=f"{service.commercial_price.amount:.2f}",
                ),
                quote_required=service.commercial_price.quote_required,
                accepting_payments=bool(registry.enabled_rails(service_id)),
                payment_options_url=f"{base}/v1/payment-options?service={service_id}",
            )
        )
        policy_urls.update(service.policy_urls)
    return Discovery(
        service="DebitMyData Agent Gateway",
        version=__version__,
        offers=offers,
        mcp_url=settings.mcp_url,
        policy_urls=policy_urls,
        keywords=list(registry.discovery_terms),
    )


@router.get(
    "/payment-options",
    response_model=PaymentOptions,
    summary="Payment options currently enabled for a service",
)
def payment_options(
    request: Request, service: str = Query(default="did_registration", max_length=64)
) -> PaymentOptions:
    registry = _registry(request)
    if service not in registry.services:
        raise HTTPException(status_code=404, detail="unknown service")
    config = registry.services[service]
    options = [
        PaymentOption(
            id=rail_id,
            name=rail.name,
            asset=rail.asset.symbol,
            network=rail.network,
            payment_protocol=rail.protocol,
            scheme=rail.scheme,
            wallet_compatibility=list(rail.wallet_compatibility),
            status=RailState.ENABLED,
        )
        for rail_id, rail in registry.enabled_rails(service)
    ]
    return PaymentOptions(
        service=config.title,
        commercial_fee=Money(
            currency=config.commercial_price.currency,
            amount=f"{config.commercial_price.amount:.2f}",
        ),
        supported_payment_options=options,
        notice=AVAILABILITY_NOTICE,
    )
