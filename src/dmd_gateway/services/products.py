"""Products bought for a DID that already exists, such as BottyBotty.

    quote     POST /v1/products/<product>/quotes      the DID, the payment option -> a quote
    pay       the agent sends the exact amount to the quote's address (like the DID itself)
    status    GET  /v1/products/purchases/<id>        payment progress, what to do next
    complete  POST /v1/products/purchases/<id>/complete  once paid: a receipt signed by
              DebitMyData and the product's entitlement on the DID, for the product's period

Only an active DID can buy, and only an active DID gets the entitlement. Buying again before
the period ends extends it from its end. A product is offered only once switched on
(``enable_env`` in the route registry).

Tiers (BottyBotty Entry, Standard, Premium) are owned for good, one per DID: the DID's one
entitlement row for the family moves up to the tier bought, and a higher tier costs the
difference between the two tiers' prices. A lower or the same tier is refused.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from dmd_gateway.adapters.base import AdapterUnavailable
from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import (
    RECIPIENT_KINDS,
    AgentIdentity,
    CreditedOutput,
    Entitlement,
    PaymentEvent,
    PaymentIntent,
    Purchase,
)
from dmd_gateway.domain.ids import PAYMENT_INTENT, PURCHASE, new_id
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.domain.routes import ProductConfig, RailConfig, TierConfig
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.credentials import ProductFacts, product_receipt
from dmd_gateway.services.identity_status import public_identity
from dmd_gateway.services.policy_pages import policy_links
from dmd_gateway.services.quotes import (
    Money,
    PriceQuote,
    QuoteContext,
    SelectedPaymentOption,
    _quote_amount,
    _token_contract,
)
from dmd_gateway.services.registrations import IssuanceContext

log = logging.getLogger("dmd_gateway.products")

PAYING = (
    PaymentStatus.DETECTED.value,
    PaymentStatus.CONFIRMING.value,
    PaymentStatus.VERIFIED.value,
)


class ProductQuoteRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "did": "did:web:api.debitmydata.io:agents:01JA8Z3T6H5K9V2M4Q7R8S0W1X",
                    "preferred_payment_option": "dmd_pow_native",
                }
            ]
        },
    )

    did: str = Field(
        min_length=26,
        max_length=300,
        description="The DID-LLM the product is for (or its identity ID, dmd_identity_...). It "
        "must be active.",
    )
    preferred_payment_option: str = Field(
        min_length=1, max_length=64, description="How the agent pays, e.g. dmd_pow_native."
    )


class TierInfo(BaseModel):
    group: str = Field(description="The family of tiers, e.g. bottybotty.")
    rank: int = Field(description="1 is the lowest tier.")
    name: str


class ProductInfo(BaseModel):
    product_id: str
    name: str
    description: str
    entitlement: str
    period_days: int | None = Field(description="How long one purchase covers; null: for good.")
    commercial_price: Money
    tier: TierInfo | None = Field(
        default=None,
        description="One tier of a family: a DID-LLM owns one tier, and a higher tier costs the "
        "difference in price.",
    )
    payment_options: list[str]
    quote_url: str


class ProductQuote(BaseModel):
    purchase_id: str
    payment_intent_id: str
    product: ProductInfo
    did: str
    commercial_price: Money = Field(description="What this quote charges.")
    list_price: Money = Field(
        description="The product's price (more than commercial_price for "
        "an upgrade, which pays the difference)."
    )
    upgrade_from: str | None = Field(
        default=None, description="For an upgrade: the tier the DID-LLM owns now (product ID)."
    )
    selected_payment_option: SelectedPaymentOption
    price_quote: PriceQuote | None
    payment_status: str
    next_action: str
    expires_at: datetime
    status_url: str
    terms_version: str
    policy_urls: dict[str, str]


class PurchaseState(BaseModel):
    purchase_id: str
    product_id: str
    did: str
    status: str = Field(description="awaiting_payment, paid, completed or failed")
    payment_intent_id: str
    payment_status: str
    next_action: str = Field(
        description="send_payment, wait_for_confirmations, complete_purchase, none (completed) "
        "or request_new_quote (failed)"
    )
    entitlement: str
    covers_until: datetime | None = Field(
        default=None, description="Once completed: the end of the period bought (null: no end)."
    )
    receipt: dict[str, Any] | None = Field(
        default=None, description="Once completed: the receipt signed by DebitMyData."
    )


def product_info(ctx: QuoteContext, product_id: str, product: ProductConfig) -> ProductInfo:
    base = ctx.public_base_url.rstrip("/")
    return ProductInfo(
        product_id=product_id,
        name=product.title,
        description=product.description,
        entitlement=product.entitlement,
        period_days=product.period_days,
        commercial_price=Money(
            currency=product.commercial_price.currency,
            amount=f"{product.commercial_price.amount:.2f}",
        ),
        tier=_tier_info(product.tier),
        payment_options=[rail_id for rail_id, _ in ctx.registry.product_rails(product_id)],
        quote_url=f"{base}{product.path}/quotes",
    )


def _tier_info(tier: TierConfig | None) -> TierInfo | None:
    return TierInfo(group=tier.group, rank=tier.rank, name=tier.name) if tier else None


def offered_products(ctx: QuoteContext) -> list[ProductInfo]:
    """The products switched on; the tiers of a family together, lowest first."""
    offered = [
        (product_id, product)
        for product_id, product in ctx.registry.products.items()
        if ctx.registry.product_on(product_id)
    ]
    offered.sort(
        key=lambda item: (item[1].tier.group, item[1].tier.rank) if item[1].tier else (item[0], 0)
    )
    return [product_info(ctx, product_id, product) for product_id, product in offered]


def product_by_path(ctx: QuoteContext, slug: str) -> tuple[str, ProductConfig]:
    for product_id, product in ctx.registry.products.items():
        if product.path == f"/v1/products/{slug}" and ctx.registry.product_on(product_id):
            return product_id, product
    raise ApiError(404, "not_found", "This product is not offered on this gateway.")


def _rail(ctx: QuoteContext, product_id: str, product: ProductConfig, rail_id: str) -> RailConfig:
    enabled = [rail for rail, _ in ctx.registry.product_rails(product_id)]
    if rail_id not in product.payment_options:
        raise ApiError(
            422,
            "unknown_payment_option",
            f"{rail_id!r} is not a payment option for this product.",
            details={"enabled_payment_options": enabled},
        )
    if rail_id not in enabled or rail_id not in ctx.adapters:
        raise ApiError(
            503,
            "payment_option_unavailable",
            f"Payment option {rail_id!r} is not available right now.",
            details={"enabled_payment_options": enabled},
            headers={"Retry-After": "60"},
        )
    return ctx.registry.rails[rail_id]


def _active_identity(session: Session, text: str, *, lock: bool = False) -> AgentIdentity:
    identity = public_identity(session, text, lock=lock)
    if identity is None:
        raise ApiError(404, "unknown_did", "No DID-LLM with this identifier on this gateway.")
    if identity.status != "active":
        raise ApiError(
            409, "did_not_active", f"This DID-LLM is {identity.status}: it cannot buy products."
        )
    return identity


def _held(
    session: Session, identity: AgentIdentity, entitlement: str, *, lock: bool = False
) -> Entitlement | None:
    query = select(Entitlement).where(
        Entitlement.subject_id == identity.id, Entitlement.entitlement == entitlement
    )
    return session.scalars(query.with_for_update() if lock else query).first()


def _check_can_hold(held: Entitlement | None) -> None:
    """Refuse a purchase that could not give anything: an entitlement revoked on its own never
    comes back, and one already held for good cannot be extended."""
    if held is None:
        return
    if held.status == "revoked":
        raise ApiError(
            409,
            "entitlement_revoked",
            "This entitlement was revoked for this DID-LLM: it cannot be bought again.",
        )
    if held.status == "active" and held.expires_at is None:
        raise ApiError(
            409, "already_covered", "This DID-LLM already holds this entitlement for good."
        )


Owned = tuple[Entitlement, str, ProductConfig]


def _owned_tier(
    session: Session, ctx: QuoteContext, identity: AgentIdentity, group: str, *, lock: bool = False
) -> Owned | None:
    """The DID's tier in a family (its entitlement row, the product ID and the product), or
    None. A tier revoked on its own never comes back: nothing more is sold for it."""
    members = {product.entitlement: (pid, product) for pid, product in ctx.registry.tiers(group)}
    query = select(Entitlement).where(
        Entitlement.subject_id == identity.id, Entitlement.entitlement.in_(list(members))
    )
    rows = session.scalars(query.with_for_update() if lock else query).all()
    for row in rows:
        if row.status == "revoked":
            _check_can_hold(row)
    if not rows:
        return None
    row = max(rows, key=lambda r: _rank(members[r.entitlement][1]))
    product_id, product = members[row.entitlement]
    return row, product_id, product


def _rank(product: ProductConfig) -> int:
    return product.tier.rank if product.tier else 0


def _tier_price(
    owned: Owned | None, product_id: str, product: ProductConfig
) -> tuple[Decimal, str | None]:
    """What a tier costs this DID: its price, or for an upgrade the difference with the tier it
    owns (and that tier's product ID). The same or a lower tier is refused."""
    if owned is None:
        return product.commercial_price.amount, None
    _, owned_id, owned_product = owned
    if _rank(owned_product) >= _rank(product):
        message = (
            f"This DID-LLM already owns {product.title}."
            if owned_id == product_id
            else f"This DID-LLM already owns {owned_product.title}, a higher tier."
        )
        raise ApiError(409, "already_covered", message, details={"owned": owned_id})
    return (
        product.commercial_price.amount - owned_product.commercial_price.amount,
        owned_id,
    )


def create_product_quote(
    session: Session,
    ctx: QuoteContext,
    product_id: str,
    product: ProductConfig,
    request: ProductQuoteRequest,
    now: datetime,
) -> ProductQuote:
    """Store the purchase and its payment intent, and return the quote (one transaction)."""
    rail_id = request.preferred_payment_option
    rail = _rail(ctx, product_id, product, rail_id)
    identity = _active_identity(session, request.did)
    upgrade_from = None
    if product.tier is not None:
        owned = _owned_tier(session, ctx, identity, product.tier.group)
        usd_price, upgrade_from = _tier_price(owned, product_id, product)
    else:
        _check_can_hold(_held(session, identity, product.entitlement))
        usd_price = product.commercial_price.amount
    amount_units, observation = _quote_amount(ctx, rail, usd_price, now)
    if amount_units <= 0:
        raise ApiError(500, "quote_failed", "The quote could not be calculated.")
    expires_at = now + ctx.quote_ttl
    purchase_id, payment_id = new_id(PURCHASE), new_id(PAYMENT_INTENT)

    session.add(
        Purchase(
            id=purchase_id,
            product_id=product_id,
            commercial_price_currency=product.commercial_price.currency,
            commercial_price_amount=usd_price,
            terms_version=product.terms_version,
            status="awaiting_payment",
            subject_identity_id=identity.id,
            subject_did=identity.public_identifier,
        )
    )
    session.flush()
    try:
        destination = ctx.adapters[rail_id].prepare_destination(payment_id)
    except AdapterUnavailable as exc:
        log.warning(
            "payment rail unavailable for product quote",
            extra={"event": {"rail": rail_id, "error": str(exc)}},
        )
        raise ApiError(
            503,
            "payment_rail_unavailable",
            f"Payment option {rail_id!r} is temporarily unavailable; try again later.",
            headers={"Retry-After": "60"},
        ) from exc
    if destination.recipient_kind not in RECIPIENT_KINDS or not destination.recipient:
        raise ApiError(500, "quote_failed", "The payment destination could not be prepared.")
    session.add(
        PaymentIntent(
            id=payment_id,
            purchase_id=purchase_id,
            rail_id=rail_id,
            adapter=rail.adapter,
            asset_symbol=rail.asset.symbol,
            asset_canonical_id=rail.asset.canonical_id,
            asset_decimals=rail.asset.decimals,
            network=rail.network,
            amount_base_units=Decimal(amount_units),
            commercial_price_currency=product.commercial_price.currency,
            commercial_price_amount=usd_price,
            price_source=observation.source if observation else None,
            price_usd_per_unit=observation.usd_per_unit if observation else None,
            price_observed_at=observation.observed_at if observation else None,
            recipient=destination.recipient,
            recipient_kind=destination.recipient_kind,
            payment_reference=destination.payment_reference,
            required_confirmations=rail.confirmation_policy.required_confirmations,
            finality_policy=rail.confirmation_policy.finality,
            quote_expires_at=expires_at,
            status=PaymentStatus.AWAITING_PAYMENT.value,
        )
    )
    session.flush()
    session.add(
        PaymentEvent(
            payment_intent_id=payment_id,
            from_status=None,
            to_status=PaymentStatus.AWAITING_PAYMENT.value,
            detail={"event": "product_quote_created"},
            observed_at=now,
        )
    )
    record_audit_event(
        session,
        action="product_quote.created",
        subject_type="purchase",
        subject_id=purchase_id,
        actor_type="agent",
        data={
            "product_id": product_id,
            "did": identity.public_identifier,
            "payment_intent_id": payment_id,
            "rail_id": rail_id,
            "amount_base_units": str(amount_units),
            "commercial_price_usd": f"{usd_price:.2f}",
            "list_price_usd": f"{product.commercial_price.amount:.2f}",
            "upgrade_from": upgrade_from,
            "terms_version": product.terms_version,
        },
    )
    base = ctx.public_base_url.rstrip("/")
    return ProductQuote(
        purchase_id=purchase_id,
        payment_intent_id=payment_id,
        product=product_info(ctx, product_id, product),
        did=identity.public_identifier,
        commercial_price=Money(
            currency=product.commercial_price.currency, amount=f"{usd_price:.2f}"
        ),
        list_price=Money(
            currency=product.commercial_price.currency,
            amount=f"{product.commercial_price.amount:.2f}",
        ),
        upgrade_from=upgrade_from,
        selected_payment_option=SelectedPaymentOption(
            id=rail_id,
            name=rail.name,
            asset=rail.asset.symbol,
            asset_type=rail.asset.asset_type,
            asset_id=rail.asset.canonical_id or "",
            token_contract=_token_contract(rail),
            network=rail.network,
            payment_protocol=rail.protocol,
            scheme=rail.scheme,
            transfer_method=rail.transfer_method,
            amount=format_base_units(amount_units, rail.asset.decimals),
            amount_base_units=str(amount_units),
            decimals=rail.asset.decimals,
            recipient=destination.recipient,
            payment_reference=destination.payment_reference,
            required_confirmations=rail.confirmation_policy.required_confirmations,
            finality=rail.confirmation_policy.finality,
        ),
        price_quote=(
            PriceQuote(
                source=observation.source,
                usd_per_unit=str(observation.usd_per_unit),
                price_observed_at=observation.observed_at,
                quoted_at=now,
                expires_at=expires_at,
            )
            if observation
            else None
        ),
        payment_status=PaymentStatus.AWAITING_PAYMENT.value,
        next_action="send_payment",
        expires_at=expires_at,
        status_url=f"{base}/v1/products/purchases/{purchase_id}",
        terms_version=product.terms_version,
        policy_urls=policy_links(product.policy_urls, ctx.public_base_url),
    )


def _purchase(session: Session, purchase_id: str, *, lock: bool = False) -> Purchase:
    purchase = session.get(Purchase, purchase_id, with_for_update=lock)
    if purchase is None or purchase.subject_did is None:
        raise ApiError(404, "not_found", "Unknown product purchase.")
    return purchase


def _intent(session: Session, purchase: Purchase) -> PaymentIntent:
    return session.scalars(
        select(PaymentIntent).where(PaymentIntent.purchase_id == purchase.id)
    ).one()


def purchase_state(session: Session, ctx: QuoteContext, purchase_id: str) -> PurchaseState:
    purchase = _purchase(session, purchase_id)
    intent = _intent(session, purchase)
    if purchase.status == "completed":
        action = "none"
    elif purchase.status == "paid":
        action = "complete_purchase"
    elif purchase.status == "failed":
        action = "request_new_quote"
    elif intent.status in PAYING:
        action = "wait_for_confirmations"
    else:
        action = "send_payment"
    product = ctx.registry.products.get(purchase.product_id)
    return PurchaseState(
        purchase_id=purchase.id,
        product_id=purchase.product_id,
        did=purchase.subject_did or "",
        status=purchase.status,
        payment_intent_id=intent.id,
        payment_status=intent.status,
        next_action=action,
        entitlement=product.entitlement if product else "",
        covers_until=purchase.covers_until,
        receipt=purchase.receipt,
    )


def complete_product_purchase(
    session: Session,
    ctx: QuoteContext,
    issuance: IssuanceContext,
    purchase_id: str,
    now: datetime,
) -> PurchaseState:
    """Issue the receipt and grant (or extend) the entitlement of a paid purchase. Idempotent:
    a completed purchase is returned as it is."""
    purchase = _purchase(session, purchase_id, lock=True)
    if purchase.status == "completed":
        return purchase_state(session, ctx, purchase_id)
    if purchase.status != "paid":
        state = purchase_state(session, ctx, purchase_id)
        raise ApiError(
            409,
            "purchase_not_ready",
            f"The purchase is {purchase.status}: it can be completed once the payment is settled.",
            details={"next_action": state.next_action, "payment_status": state.payment_status},
        )
    product = ctx.registry.products.get(purchase.product_id)
    if product is None:
        raise ApiError(409, "product_withdrawn", "This product is no longer configured.")
    if issuance.issuer is None:
        raise ApiError(
            503, "issuance_unavailable", "Receipts cannot be signed right now; try again later."
        )
    identity = _active_identity(session, purchase.subject_identity_id or "", lock=True)
    intent = _intent(session, purchase)
    transactions = sorted(
        set(
            session.scalars(
                select(CreditedOutput.transaction_reference).where(
                    CreditedOutput.payment_intent_id == intent.id
                )
            )
        )
    )
    now = now.replace(microsecond=0)  # equal to the receipt's signed validFrom
    start, until, upgrade_from = now, None, None
    if product.tier is not None:
        owned = _owned_tier(session, ctx, identity, product.tier.group, lock=True)
        try:
            _, upgrade_from = _tier_price(owned, purchase.product_id, product)
        except ApiError as exc:
            # Another purchase completed first with this tier or a higher one.
            raise ApiError(
                409,
                "already_covered",
                f"{exc.message} This paid purchase cannot be completed: it is refunded by hand "
                "(see the refund policy).",
                details={**exc.details, "next_action": "contact_support_for_refund"},
            ) from exc
        entitlement = owned[0] if owned else None
    else:
        entitlement = _held(session, identity, product.entitlement, lock=True)
        if entitlement is not None and entitlement.status == "revoked":
            _check_can_hold(entitlement)
        active = entitlement is not None and entitlement.status == "active"
        end = entitlement.expires_at if entitlement is not None else None
        # Already held for good (two purchases paid at once): nothing shortens it.
        if not (active and end is None):
            if active and end is not None and end > now:
                start = end  # buying again before the end extends the period
            if product.period_days:
                until = start + timedelta(days=product.period_days)
    base = issuance.public_base_url.rstrip("/")
    receipt = product_receipt(
        ProductFacts(
            receipt_url=f"{base}/v1/products/purchases/{purchase.id}",
            purchase_id=purchase.id,
            product_id=purchase.product_id,
            product_name=product.title,
            entitlement=product.entitlement,
            did=identity.public_identifier,
            currency=purchase.commercial_price_currency,
            amount=f"{purchase.commercial_price_amount:.2f}",
            terms_version=purchase.terms_version,
            covers_from=start,
            covers_until=until,
            payment_intent_id=intent.id,
            payment_option=intent.rail_id,
            asset=intent.asset_symbol,
            network=intent.network,
            amount_paid=format_base_units(int(intent.amount_base_units), intent.asset_decimals),
            recipient=intent.recipient,
            transactions=transactions,
            issued_at=now,
            list_amount=f"{product.commercial_price.amount:.2f}",
            upgrade_from=upgrade_from,
            tier=(
                {"group": product.tier.group, "rank": product.tier.rank, "name": product.tier.name}
                if product.tier
                else None
            ),
        ),
        issuance.issuer,
    )
    if entitlement is None:
        session.add(
            Entitlement(
                subject_id=identity.id,
                entitlement=product.entitlement,
                status="active",
                expires_at=until,
            )
        )
    else:
        if product.tier is not None:
            # The DID's row for the family moves up to this tier, from now.
            entitlement.entitlement = product.entitlement
            entitlement.granted_at = now
        entitlement.status = "active"
        entitlement.revoked_at = None
        entitlement.expires_at = until
    purchase.receipt = receipt
    purchase.status = "completed"
    purchase.completed_at = now
    purchase.covers_until = until
    purchase.updated_at = now
    record_audit_event(
        session,
        action="purchase.completed",
        subject_type="purchase",
        subject_id=purchase.id,
        actor_type="agent",
        data={
            "product_id": purchase.product_id,
            "did": identity.public_identifier,
            "payment_intent_id": intent.id,
            "transactions": transactions,
        },
    )
    record_audit_event(
        session,
        action="entitlement.granted",
        subject_type="identity",
        subject_id=identity.id,
        actor_type="system",
        data={
            "entitlement": product.entitlement,
            "upgrade_from": upgrade_from,
            "purchase_id": purchase.id,
            "covers_from": start.isoformat(),
            "covers_until": until.isoformat() if until else None,
        },
    )
    session.flush()
    return purchase_state(session, ctx, purchase_id)
