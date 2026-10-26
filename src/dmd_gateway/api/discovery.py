"""Public discovery endpoints (DOC-04, DOC-05).

These describe only what is actually live: a payment option appears only when its rail is
enabled, and the MCP endpoint appears only once it is configured.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from dmd_gateway import __version__
from dmd_gateway.domain.routes import PaymentRouteRegistry, RailConfig, RailState
from dmd_gateway.services.policy_pages import policy_links

router = APIRouter(prefix="/v1", tags=["discovery"])

QUOTE_PATHS = {"did_registration": "/v1/dids/registration-quotes"}

# What these endpoints say only changes when the gateway restarts with a new configuration,
# so agents and caches in front of the gateway (such as Cloudflare) may keep it a minute.
CACHE_FOR_A_MINUTE = "public, max-age=60"

# Wording as specified in the directive's payment-options response.
AVAILABILITY_NOTICE = (
    "Payment availability may vary by asset, network, wallet policy, geographic eligibility, "
    "and DebitMyData risk controls."
)


class Money(BaseModel):
    currency: str
    amount: str


class Endpoint(BaseModel):
    method: str
    url: str
    available: bool


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
    endpoints: dict[str, Endpoint]


class AmountRange(BaseModel):
    currency: str
    minimum: str
    maximum: str


class DonationOffer(BaseModel):
    """A donation: the donor chooses the amount within the range, and pays in one call."""

    id: str
    title: str
    description: str
    method: str
    path: str
    resource: str
    amount: AmountRange
    accepting_payments: bool
    payment_options_url: str
    endpoints: dict[str, Endpoint]


class CredentialIssuer(BaseModel):
    did: str
    did_document_url: str


class Discovery(BaseModel):
    service: str
    version: str
    offers: list[Offer]
    donations: list[DonationOffer]
    mcp_url: str | None
    credential_issuer: CredentialIssuer | None
    policy_urls: dict[str, str]
    keywords: list[str]


class PaymentOption(BaseModel):
    id: str
    name: str
    asset: str
    asset_id: str | None
    token_contract: str | None
    decimals: int
    network: str
    payment_protocol: str
    scheme: str | None
    transfer_method: str | None
    wallet_compatibility: list[str]
    status: RailState


class PaymentOptions(BaseModel):
    service: str
    commercial_fee: Money | None  # a fixed price; None for a donation
    donation_amount: AmountRange | None = None  # the range a donor may choose from
    supported_payment_options: list[PaymentOption]
    notice: str


def _transfer_method(rail: RailConfig) -> str | None:
    if rail.protocol == "x402":
        return rail.transfer_method or "eip3009"  # the x402 "exact" scheme's default
    return rail.transfer_method


def _registry(request: Request) -> PaymentRouteRegistry:
    return request.app.state.registry


@router.get("/discovery", response_model=Discovery, summary="Describe DebitMyData services")
def discovery(request: Request, response: Response) -> Discovery:
    response.headers["Cache-Control"] = CACHE_FOR_A_MINUTE
    registry = _registry(request)
    settings = request.app.state.settings
    base = settings.public_base_url.rstrip("/")
    offers = []
    policy_urls: dict[str, str] = {}
    issuing = request.app.state.issuance.available
    for service_id, service in registry.services.items():
        accepting = bool(registry.enabled_rails(service_id))
        registration = f"{base}/v1/dids/registrations/{{registration_intent_id}}"
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
                accepting_payments=accepting,
                payment_options_url=f"{base}/v1/payment-options?service={service_id}",
                endpoints={
                    "quote": Endpoint(
                        method="POST",
                        url=f"{base}{QUOTE_PATHS[service_id]}",
                        available=accepting,
                    ),
                    "registration_status": Endpoint(
                        method="GET", url=registration, available=accepting
                    ),
                    "controller_challenge": Endpoint(
                        method="POST",
                        url=f"{registration}/controller-challenges",
                        available=accepting,
                    ),
                    "controller_proof": Endpoint(
                        method="POST",
                        url=f"{registration}/controller-proofs",
                        available=accepting,
                    ),
                    # Issues the DID once payment is settled and the controller proof verified.
                    "registration": Endpoint(
                        method=service.method,
                        url=f"{base}{service.path}",
                        available=accepting and issuing,
                    ),
                },
            )
        )
        policy_urls.update(policy_links(service.policy_urls, base))
    donations = [
        _donation_offer(registry, donation_id, base, issuing) for donation_id in registry.donations
    ]
    return Discovery(
        service="DebitMyData Agent Gateway",
        version=__version__,
        offers=offers,
        donations=donations,
        mcp_url=settings.mcp_url or f"{base}/mcp",
        credential_issuer=(
            CredentialIssuer(
                did=request.app.state.issuance.issuer.did,
                did_document_url=f"{base}/.well-known/did.json",
            )
            if issuing
            else None
        ),
        policy_urls=policy_urls,
        keywords=list(registry.discovery_terms),
    )


def _amount_range(registry: PaymentRouteRegistry, donation_id: str) -> AmountRange:
    amount = registry.donations[donation_id].amount
    return AmountRange(
        currency=amount.currency, minimum=f"{amount.minimum:.2f}", maximum=f"{amount.maximum:.2f}"
    )


def _donation_offer(
    registry: PaymentRouteRegistry, donation_id: str, base: str, issuing: bool
) -> DonationOffer:
    donation = registry.donations[donation_id]
    accepting = bool(registry.donation_rails(donation_id)) and issuing
    return DonationOffer(
        id=donation_id,
        title=donation.title,
        description=" ".join(donation.description.split()),
        method=donation.method,
        path=donation.path,
        resource=f"{base}{donation.path}",
        amount=_amount_range(registry, donation_id),
        accepting_payments=accepting,
        payment_options_url=f"{base}/v1/payment-options?service={donation_id}",
        endpoints={
            "donate": Endpoint(method="POST", url=f"{base}{donation.path}", available=accepting),
            "donation_status": Endpoint(
                method="GET", url=f"{base}/v1/donations/{{purchase_id}}", available=accepting
            ),
        },
    )


def _option(rail_id: str, rail: RailConfig) -> PaymentOption:
    return PaymentOption(
        id=rail_id,
        name=rail.name,
        asset=rail.asset.symbol,
        asset_id=rail.asset.canonical_id,
        token_contract=rail.token_contract,
        decimals=rail.asset.decimals,
        network=rail.network,
        payment_protocol=rail.protocol,
        scheme=rail.scheme,
        transfer_method=_transfer_method(rail),
        wallet_compatibility=list(rail.wallet_compatibility),
        status=RailState.ENABLED,
    )


@router.get(
    "/payment-options",
    response_model=PaymentOptions,
    summary="Payment options currently enabled for a service",
)
def payment_options(
    request: Request,
    response: Response,
    service: str = Query(
        default="did_registration",
        max_length=64,
        description="A service or donation ID from /v1/discovery",
    ),
) -> PaymentOptions:
    response.headers["Cache-Control"] = CACHE_FOR_A_MINUTE
    registry = _registry(request)
    if service in registry.donations:
        return PaymentOptions(
            service=registry.donations[service].title,
            commercial_fee=None,
            donation_amount=_amount_range(registry, service),
            supported_payment_options=[
                _option(rail_id, rail) for rail_id, rail, _ in registry.donation_rails(service)
            ],
            notice=AVAILABILITY_NOTICE,
        )
    if service not in registry.services:
        raise HTTPException(status_code=404, detail="unknown service")
    config = registry.services[service]
    options = [_option(rail_id, rail) for rail_id, rail in registry.enabled_rails(service)]
    return PaymentOptions(
        service=config.title,
        commercial_fee=Money(
            currency=config.commercial_price.currency,
            amount=f"{config.commercial_price.amount:.2f}",
        ),
        supported_payment_options=options,
        notice=AVAILABILITY_NOTICE,
    )
