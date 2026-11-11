"""Products bought for a DID-LLM that already exists, such as a BottyBotty tier.

GET  /v1/products                                  the products offered, with price and period
POST /v1/products/{product}/quotes                 a quote for one DID-LLM (active)
GET  /v1/products/purchases/{purchase_id}          payment progress and what to do next
POST /v1/products/purchases/{purchase_id}/complete once paid: the signed receipt and the
                                                   entitlement on the DID-LLM
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Path, Request

from dmd_gateway.db.session import transaction
from dmd_gateway.services.products import (
    ProductInfo,
    ProductQuote,
    ProductQuoteRequest,
    PurchaseState,
    complete_product_purchase,
    create_product_quote,
    offered_products,
    product_by_path,
    purchase_state,
)

router = APIRouter(prefix="/v1/products", tags=["products"])

PURCHASE_ID = r"^dmd_purchase_[0-9A-HJKMNP-TV-Z]{26}$"


@router.get(
    "",
    response_model=list[ProductInfo],
    summary="Products an agent can buy for its DID-LLM (such as the BottyBotty tiers)",
)
def products(request: Request) -> list[ProductInfo]:
    return offered_products(request.app.state.quote_context)


@router.post(
    "/{product}/quotes",
    status_code=201,
    response_model=ProductQuote,
    summary="Quote a product for an active DID-LLM: the amount and where to pay it",
    responses={
        404: {"description": "Unknown product or DID-LLM."},
        409: {"description": "The DID-LLM is suspended or revoked."},
    },
)
def quote(
    request: Request,
    body: ProductQuoteRequest,
    product: str = Path(pattern=r"^[a-z][a-z0-9-]{1,40}$", description="e.g. bottybotty-entry"),
) -> ProductQuote:
    ctx = request.app.state.quote_context
    product_id, config = product_by_path(ctx, product)
    with transaction(request.app.state.session_factory) as session:
        return create_product_quote(session, ctx, product_id, config, body, datetime.now(UTC))


@router.get(
    "/purchases/{purchase_id}",
    response_model=PurchaseState,
    summary="A product purchase: payment progress, next action, and its receipt once completed",
    responses={404: {"description": "Unknown product purchase."}},
)
def status(request: Request, purchase_id: str = Path(pattern=PURCHASE_ID)) -> PurchaseState:
    with transaction(request.app.state.session_factory) as session:
        return purchase_state(session, request.app.state.quote_context, purchase_id)


@router.post(
    "/purchases/{purchase_id}/complete",
    response_model=PurchaseState,
    summary="Once paid: issue the signed receipt and the entitlement on the DID-LLM (idempotent)",
    responses={
        404: {"description": "Unknown product purchase."},
        409: {"description": "Not paid yet, or the DID-LLM is no longer active."},
    },
)
def complete(request: Request, purchase_id: str = Path(pattern=PURCHASE_ID)) -> PurchaseState:
    state = request.app.state
    with transaction(state.session_factory) as session:
        return complete_product_purchase(
            session, state.quote_context, state.issuance, purchase_id, datetime.now(UTC)
        )
