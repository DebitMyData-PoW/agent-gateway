"""Staging-only test tools, so the whole registration can be tried from the API docs page.

    POST /v1/staging/test-agent-keys    a throwaway agent key and wallet (the agent's own in
                                        production)
    POST /v1/staging/test-payments      pay a quote with test coins (DMD: send and mine the
                                        confirmations; x402: sign the payment)
    POST /v1/staging/test-donation-payments
                                        sign a donation's x402 payment (test USDC)
    POST /v1/staging/test-signatures    sign a controller challenge with the throwaway key or
                                        wallet

Only mounted when ``staging_test_tools`` is on, which settings refuse in production. See
``dmd_gateway.services.staging_tools`` for the other safeguards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account.signers.local import LocalAccount
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from dmd_gateway.adapters.x402 import X402PaymentAdapter
from dmd_gateway.api.donations import donation_context
from dmd_gateway.api.payments import ID_PATTERN as PAYMENT_ID
from dmd_gateway.api.payments import PAYMENT_ID_HELP, unknown_payment
from dmd_gateway.api.registrations import CHALLENGE_ID
from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import AuditEvent, ControllerChallenge, PaymentIntent
from dmd_gateway.db.session import transaction
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import encode_signature
from dmd_gateway.identity.wallets import WalletError, wallet_from_account_id
from dmd_gateway.services.controller_proofs import EIP155
from dmd_gateway.services.donations import AMOUNT_PATTERN, donation_payment_terms
from dmd_gateway.services.payment_references import X402_ADAPTER
from dmd_gateway.services.quotes import DID_REGISTRATION
from dmd_gateway.services.staging_tools import (
    BASE_SEPOLIA,
    BaseSepoliaTestPayer,
    RegtestPayer,
    StagingToolError,
    is_wallet_key,
    load_test_agent_key,
    load_test_wallet,
    new_test_agent_key,
    public_key_of,
    sign_with_test_wallet,
)
from dmd_gateway.services.x402_payments import encode_header, payment_terms, resource_info

router = APIRouter(prefix="/v1/staging", tags=["staging"])

TEST_RAIL = "dmd_pow_native"
TEST_NETWORK = "dmd:regtest"
PAYMENT_ACTION = "staging.test_payment_sent"
X402_SIGNED_ACTION = "staging.test_x402_payment_signed"
DONATION_SIGNED_ACTION = "staging.test_donation_payment_signed"


# --------------------------------------------------------------------------- agent key


class ThrowawayKeyResponse(BaseModel):
    controller_public_key: str = Field(
        description="Put this in controller_public_key when you request a quote."
    )
    private_key: str = Field(description="Keep it for POST /v1/staging/test-signatures.")
    controller_wallet_address: str = Field(
        description="A throwaway EVM wallet: put it in controller_wallet_address when you "
        "request a quote, to try the wallet proof (with or instead of the key)."
    )
    wallet_private_key: str = Field(
        description="The wallet's private key: keep it for POST /v1/staging/test-signatures."
    )
    note: str


@router.post(
    "/test-agent-keys",
    status_code=201,
    response_model=ThrowawayKeyResponse,
    summary="Staging only: create a throwaway agent key and wallet",
    description=(
        "Stands in for the agent's own Ed25519 key and EVM wallet so the flow can be tried "
        "from this page. A real agent creates them itself and never shares the private keys. "
        "The wallet holds nothing: it only signs. Nothing is stored."
    ),
)
def create_test_agent_key() -> ThrowawayKeyResponse:
    key = new_test_agent_key()
    return ThrowawayKeyResponse(
        controller_public_key=key.controller_public_key,
        private_key=key.private_key,
        controller_wallet_address=key.wallet_address,
        wallet_private_key=key.wallet_private_key,
        note="Test key and wallet for the staging server only; the gateway does not keep them.",
    )


# --------------------------------------------------------------------------- payment


class StagingPaymentRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"examples": [{"payment_intent_id": "dmd_payment_intent_..."}]},
    )

    payment_intent_id: str = Field(pattern=PAYMENT_ID, description=PAYMENT_ID_HELP)


class StagingPaymentResponse(BaseModel):
    payment_intent_id: str
    amount: str
    asset: str
    recipient: str
    # DMD quotes: the test coins were sent and confirmed.
    transaction_reference: str | None = None
    blocks_mined: int | None = None
    # x402 quotes: a payment signed by the test wallet; nothing moves until it is used.
    payment_signature: str | None = Field(
        default=None,
        description="x402 quotes: the signed payment (long, it starts with eyJ). Put it in "
        "the PAYMENT-SIGNATURE header of POST /v1/dids/registrations before valid_until.",
    )
    payer: str | None = None
    valid_until: datetime | None = None
    next_step: str


@router.post(
    "/test-payments",
    status_code=201,
    response_model=StagingPaymentResponse,
    summary="Staging only: pay a quote with test coins",
    description=(
        "Stands in for the agent's own wallet, on test networks whose coins have no value. "
        "**DMD quotes:** the staging test wallet sends the exact quoted amount to the quoted "
        "address on the private regtest chain, then mines the required confirmations; the "
        "gateway then verifies the payment on chain like any other (check "
        "GET /v1/payments/{payment_intent_id}). **x402 quotes (USDC on Base Sepolia):** the "
        "staging test wallet signs the x402 payment for the quote and returns it as "
        "`payment_signature`; send it in the PAYMENT-SIGNATURE header of "
        "POST /v1/dids/registrations, after proving the controller key."
    ),
    responses={
        404: {"description": "Unknown payment intent."},
        409: {"description": "Already paid, expired, or not a test-network quote."},
        503: {"description": "The test wallet or its node is not available."},
    },
)
def create_test_payment(body: StagingPaymentRequest, request: Request) -> StagingPaymentResponse:
    now = datetime.now(UTC)
    with transaction(request.app.state.session_factory) as session:
        intent = session.scalars(
            select(PaymentIntent)
            .where(PaymentIntent.id == body.payment_intent_id)
            .with_for_update()
        ).one_or_none()
        if intent is None:
            raise unknown_payment(session, body.payment_intent_id)
        if intent.adapter == X402_ADAPTER:
            return _sign_x402_test_payment(session, request, intent, now)
        if intent.rail_id != TEST_RAIL or intent.network != TEST_NETWORK:
            raise ApiError(
                409,
                "not_a_test_payment",
                "Only quotes on the DebitMyData regtest chain can be test-paid.",
            )
        _check_open(intent, now)
        already = session.scalar(
            select(AuditEvent.id)
            .where(AuditEvent.action == PAYMENT_ACTION, AuditEvent.subject_id == intent.id)
            .limit(1)
        )
        if already is not None:
            raise ApiError(
                409,
                "already_test_paid",
                "This quote was already paid with test coins; check its payment status.",
            )

        amount = Decimal(intent.amount_base_units) / Decimal(10) ** intent.asset_decimals
        amount = amount.quantize(Decimal(1).scaleb(-intent.asset_decimals))
        confirmations = intent.required_confirmations or 6
        try:
            payment = _payer(request).pay(intent.recipient, amount, confirmations)
        except StagingToolError as exc:
            raise ApiError(503, "test_wallet_unavailable", str(exc)) from exc
        record_audit_event(
            session,
            action=PAYMENT_ACTION,
            subject_type="payment_intent",
            subject_id=intent.id,
            actor_type="system",
            data={
                "transaction_reference": payment.transaction_reference,
                "blocks_mined": payment.blocks_mined,
                "topped_up": payment.topped_up,
            },
        )
        return StagingPaymentResponse(
            payment_intent_id=intent.id,
            transaction_reference=payment.transaction_reference,
            amount=str(amount),
            asset=intent.asset_symbol,
            recipient=intent.recipient,
            blocks_mined=payment.blocks_mined,
            next_step=(
                "Wait about 10 seconds, then GET /v1/payments/"
                f"{intent.id}: its status becomes settled."
            ),
        )


def _check_open(intent: PaymentIntent, now: datetime) -> None:
    if intent.status != PaymentStatus.AWAITING_PAYMENT:
        raise ApiError(
            409,
            "not_awaiting_payment",
            f"This payment is {intent.status}; only an unpaid quote can be test-paid.",
        )
    if intent.quote_expires_at <= now:
        raise ApiError(409, "quote_expired", "This quote has expired; request a new one.")


def _sign_x402_test_payment(
    session: Session, request: Request, intent: PaymentIntent, now: datetime
) -> StagingPaymentResponse:
    if intent.network != BASE_SEPOLIA:
        raise ApiError(
            409, "not_a_test_payment", "Only x402 quotes on Base Sepolia can be test-paid."
        )
    _check_open(intent, now)
    state = request.app.state
    adapter = state.quote_context.adapters.get(intent.rail_id)
    if not isinstance(adapter, X402PaymentAdapter):
        raise ApiError(503, "test_wallet_unavailable", "The x402 payment option is not enabled.")
    service = state.registry.services[DID_REGISTRATION]
    resource = resource_info(state.settings.public_base_url, service.path, service.title)
    requirements, required = payment_terms(
        adapter, intent, resource, state.settings.public_base_url, now
    )
    try:
        payer = BaseSepoliaTestPayer.from_environ(state.environ)
        signed = payer.sign(
            requirements, required["resource"], required["extensions"], int(now.timestamp())
        )
    except StagingToolError as exc:
        raise ApiError(503, "test_wallet_unavailable", str(exc)) from exc
    record_audit_event(
        session,
        action=X402_SIGNED_ACTION,
        subject_type="payment_intent",
        subject_id=intent.id,
        actor_type="system",
        data={"payer": signed.payer, "valid_before": signed.valid_before},
    )
    amount = format_base_units(int(intent.amount_base_units), intent.asset_decimals)
    valid_until = datetime.fromtimestamp(signed.valid_before, UTC)
    return StagingPaymentResponse(
        payment_intent_id=intent.id,
        amount=amount,
        asset=intent.asset_symbol,
        recipient=intent.recipient,
        payment_signature=encode_header(signed.payment_payload),
        payer=signed.payer,
        valid_until=valid_until,
        next_step=(
            "Once the controller proof is verified, POST /v1/dids/registrations with "
            f'{{"registration_intent_id": "{intent.registration_intent_id}"}} and this '
            "payment_signature in the PAYMENT-SIGNATURE header, before "
            f"{valid_until:%H:%M:%S} UTC. The payment is settled on Base Sepolia and the DID "
            "is issued in the same answer."
        ),
    )


class StagingDonationPaymentRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"examples": [{"amount_usd": "5.00"}]},
    )

    amount_usd: str = Field(
        pattern=AMOUNT_PATTERN,
        description='The donation in US dollars, e.g. "5.00": the same amount_usd as in '
        "POST /v1/donations/inference.",
    )
    donation_id: str = Field(
        default="inference_donation",
        pattern=r"^[a-z][a-z0-9_]{1,40}$",
        description="The donation, as listed in GET /v1/discovery (inference_donation).",
    )


class StagingDonationPaymentResponse(BaseModel):
    donation_id: str
    amount_usd: str
    asset: str
    network: str
    recipient: str
    payment_signature: str = Field(
        description="The signed payment (long, it starts with eyJ). Put it in the "
        "PAYMENT-SIGNATURE header of the donation, with the same amount_usd, before "
        "valid_until."
    )
    payer: str
    valid_until: datetime
    next_step: str


@router.post(
    "/test-donation-payments",
    status_code=201,
    response_model=StagingDonationPaymentResponse,
    summary="Staging only: sign a donation's x402 payment with test USDC",
    description=(
        "Stands in for the agent's own wallet, on Base Sepolia (test USDC, no value): the "
        "staging test wallet signs the x402 payment of a donation of `amount_usd`, exactly "
        "as the 402 of POST /v1/donations/inference asks for it. Nothing moves until the "
        "signed payment is used."
    ),
    responses={
        404: {"description": "Unknown donation."},
        409: {"description": "Donations on this server are not paid on a test network."},
        422: {"description": "amount_usd is outside the accepted range."},
        503: {"description": "Donations or the test wallet are not available."},
    },
)
def create_test_donation_payment(
    body: StagingDonationPaymentRequest, request: Request
) -> StagingDonationPaymentResponse:
    state = request.app.state
    donation = state.registry.donations.get(body.donation_id)
    if donation is None:
        raise ApiError(404, "not_found", "Unknown donation; see GET /v1/discovery.")
    now = datetime.now(UTC)
    requirements, required = donation_payment_terms(
        donation_context(request), body.donation_id, body.amount_usd
    )
    if requirements["network"] != BASE_SEPOLIA:
        raise ApiError(
            409, "not_a_test_payment", "Only donations paid on Base Sepolia can be test-signed."
        )
    try:
        payer = BaseSepoliaTestPayer.from_environ(state.environ)
        signed = payer.sign(
            requirements, required["resource"], required["extensions"], int(now.timestamp())
        )
    except StagingToolError as exc:
        raise ApiError(503, "test_wallet_unavailable", str(exc)) from exc
    with transaction(state.session_factory) as session:
        record_audit_event(
            session,
            action=DONATION_SIGNED_ACTION,
            subject_type="donation",
            subject_id=body.donation_id,
            actor_type="system",
            data={
                "payer": signed.payer,
                "amount": requirements["amount"],
                "valid_before": signed.valid_before,
            },
        )
    amount = Decimal(body.amount_usd).quantize(Decimal("0.01"))
    valid_until = datetime.fromtimestamp(signed.valid_before, UTC)
    return StagingDonationPaymentResponse(
        donation_id=body.donation_id,
        amount_usd=f"{amount:.2f}",
        asset=next(
            rail.asset.symbol
            for _, rail, _ in state.registry.donation_rails(body.donation_id)
            if rail.network == requirements["network"]
        ),
        network=requirements["network"],
        recipient=requirements["payTo"],
        payment_signature=encode_header(signed.payment_payload),
        payer=signed.payer,
        valid_until=valid_until,
        next_step=(
            f'POST {donation.path} with {{"amount_usd": "{amount:.2f}"}} and this '
            f"payment_signature in the PAYMENT-SIGNATURE header, before {valid_until:%H:%M:%S} "
            "UTC. The payment is settled on Base Sepolia and the answer carries the signed "
            "donation receipt."
        ),
    )


def _payer(request: Request) -> RegtestPayer:
    state = request.app.state
    if state.staging_payer is None:
        try:
            state.staging_payer = RegtestPayer.from_environ(
                state.environ, state.settings.staging_payer_wallet
            )
        except StagingToolError as exc:
            raise ApiError(503, "test_wallet_unavailable", str(exc)) from exc
    return state.staging_payer


# --------------------------------------------------------------------------- signature


class StagingSignatureRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [{"challenge_id": "dmd_challenge_...", "private_key": "z3u2..."}]
        },
    )

    challenge_id: str = Field(
        pattern=CHALLENGE_ID, description="`challenge_id` from the controller challenge."
    )
    private_key: str = Field(
        min_length=40,
        max_length=66,
        description="From test-agent-keys: private_key (z3u2...) for a key challenge, or "
        "wallet_private_key (0x...) for a wallet challenge.",
    )


class StagingSignatureResponse(BaseModel):
    challenge_id: str
    signature: str = Field(
        description="The controller signature (short; 0x... for a wallet): send it with "
        "challenge_id to POST /v1/dids/registrations/{registration_intent_id}/"
        "controller-proofs. It is not an x402 payment."
    )
    matches_controller: bool = Field(
        description="Whether this key or wallet is the challenge's controller. If not, the "
        "gateway will refuse the proof (useful to see a wrong key being refused)."
    )
    next_step: str


@router.post(
    "/test-signatures",
    response_model=StagingSignatureResponse,
    summary="Staging only: sign a controller challenge with the throwaway key or wallet",
    description=(
        "Stands in for the agent signing the challenge message with its own key, or with its "
        "wallet (personal_sign, as wallets sign text). Use private_key for a challenge whose "
        "controller is ed25519:..., wallet_private_key for one whose controller is "
        "eip155:.... Send the signature to "
        "POST /v1/dids/registrations/{registration_intent_id}/controller-proofs."
    ),
    responses={
        404: {"description": "Unknown challenge."},
        409: {"description": "The challenge was already used or has expired."},
        422: {
            "description": "The private key is not one from test-agent-keys, or not the kind "
            "this challenge needs."
        },
    },
)
def create_test_signature(
    body: StagingSignatureRequest, request: Request
) -> StagingSignatureResponse:
    try:
        signer = (
            load_test_wallet(body.private_key)
            if is_wallet_key(body.private_key)
            else load_test_agent_key(body.private_key)
        )
    except StagingToolError as exc:
        raise ApiError(422, "invalid_private_key", str(exc)) from exc
    with transaction(request.app.state.session_factory) as session:
        challenge = session.get(ControllerChallenge, body.challenge_id)
        if challenge is None:
            raise ApiError(404, "not_found", "Unknown challenge.")
        if challenge.used_at is not None or challenge.expires_at <= datetime.now(UTC):
            raise ApiError(
                409, "challenge_closed", "This challenge was used or has expired; get a new one."
            )
        signature, matches = _sign(challenge, signer)
        return StagingSignatureResponse(
            challenge_id=challenge.id,
            signature=signature,
            matches_controller=matches,
            next_step=(
                "POST /v1/dids/registrations/"
                f"{challenge.registration_intent_id}/controller-proofs with this challenge_id "
                "and signature."
            ),
        )


def _sign(
    challenge: ControllerChallenge, signer: LocalAccount | Ed25519PrivateKey
) -> tuple[str, bool]:
    """(signature, whether the signer is the challenge's controller)."""
    wallet_challenge = challenge.controller_type == EIP155
    if wallet_challenge != isinstance(signer, LocalAccount):
        needed = "wallet_private_key (0x...)" if wallet_challenge else "private_key (z3u2...)"
        raise ApiError(
            422,
            "invalid_private_key",
            f"This challenge is for {challenge.controller_value}; sign it with the {needed} "
            "from test-agent-keys.",
        )
    if isinstance(signer, LocalAccount):
        try:
            controller: str | None = wallet_from_account_id(challenge.controller_value).address
        except WalletError:
            controller = None
        return sign_with_test_wallet(signer, challenge.message), signer.address == controller
    signature = encode_signature(signer.sign(challenge.message.encode("utf-8")))
    return signature, public_key_of(signer) == challenge.controller_value
