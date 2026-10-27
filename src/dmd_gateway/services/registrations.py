"""DID registration completion (ID-08..ID-11; directive tool dmd_register_did).

``complete_registration`` issues the identity only when the single issuance rule in
``domain/issuance.py`` passes:

* the payment for the registration is ``settled``;
* a controller proof is ``verified`` for each controller named in the quote (its Ed25519
  public key and/or its EVM wallet);
* policy checks pass: the payment rail is still enabled (a killed rail also stops issuance),
  the settled outputs are credited to this payment and to nothing else, and the
  registration is not held for review;
* the registration intent is valid and not already used.

Issuance creates, in one transaction: the identity (``did:web``), the signed registration
credential (also the signed receipt), the ``dmd_did_holder`` entitlement and audit events.
Repeating the call returns the identity that was already issued, so retries are safe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import (
    AgentIdentity,
    ControllerProof,
    Credential,
    CreditedOutput,
    Entitlement,
    PaymentIntent,
    RegistrationIntent,
)
from dmd_gateway.domain.ids import CREDENTIAL, IDENTITY, new_id
from dmd_gateway.domain.issuance import ControllerProofStatus, IssuanceInputs, issuance_blockers
from dmd_gateway.domain.money import format_base_units
from dmd_gateway.domain.payment_status import NEXT_ACTIONS, PaymentStatus
from dmd_gateway.domain.routes import PaymentRouteRegistry, RailState
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.credentials import (
    CREDENTIAL_FORMAT,
    RegistrationFacts,
    registration_credential,
)
from dmd_gateway.identity.crypto import parse_controller_key
from dmd_gateway.identity.did import agent_did
from dmd_gateway.identity.issuer import Issuer
from dmd_gateway.identity.wallets import Wallet
from dmd_gateway.services.controller_proofs import (
    ED25519,
    EIP155,
    declared_controllers,
    verified_proofs,
)

DID_HOLDER = "dmd_did_holder"
INVALID_REGISTRATION_STATUSES = frozenset({"expired", "rejected", "cancelled"})
# x402: once the controller is proven, POST /v1/dids/registrations answers with HTTP 402
# and the agent pays there (the payment is settled and checked before the DID is issued).
X402_PAY_ACTION = "register_did_with_x402_payment"


@dataclass(frozen=True)
class IssuanceContext:
    registry: PaymentRouteRegistry
    issuer: Issuer | None
    public_base_url: str

    @property
    def available(self) -> bool:
        return self.issuer is not None


@dataclass
class RegistrationState:
    registration: RegistrationIntent
    payment: PaymentIntent | None
    proofs: Mapping[str, ControllerProof]  # controller type -> verified proof
    controllers: tuple[str, ...]  # controller types that must prove control
    identity: AgentIdentity | None
    outputs: list[CreditedOutput]
    blockers: list[str] = field(default_factory=list)
    next_action: str = ""

    @property
    def payment_status(self) -> PaymentStatus:
        return PaymentStatus(self.payment.status) if self.payment else PaymentStatus.CREATED

    @property
    def proven(self) -> bool:
        return bool(self.controllers) and all(kind in self.proofs for kind in self.controllers)

    @property
    def proof_status(self) -> ControllerProofStatus:
        return ControllerProofStatus.VERIFIED if self.proven else ControllerProofStatus.PENDING

    @property
    def unproven(self) -> list[str]:
        return [kind for kind in self.controllers if kind not in self.proofs]

    @property
    def proof(self) -> ControllerProof | None:
        """The proof that binds the identity: the public key's, or else the wallet's."""
        if not self.proven:
            return None
        return self.proofs.get(ED25519) or self.proofs.get(EIP155)


def payment_for(
    session: Session, registration_intent_id: str, *, lock: bool
) -> PaymentIntent | None:
    query = (
        select(PaymentIntent)
        .where(PaymentIntent.registration_intent_id == registration_intent_id)
        .order_by(PaymentIntent.created_at.desc(), PaymentIntent.id.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update()
    return session.scalars(query).first()


def evaluate(
    session: Session, ctx: IssuanceContext, registration: RegistrationIntent
) -> RegistrationState:
    payment = payment_for(session, registration.id, lock=False)
    identity = session.scalars(
        select(AgentIdentity).where(AgentIdentity.registration_intent_id == registration.id)
    ).first()
    outputs: list[CreditedOutput] = []
    if payment is not None:
        outputs = list(
            session.scalars(
                select(CreditedOutput)
                .where(CreditedOutput.payment_intent_id == payment.id)
                .order_by(CreditedOutput.transaction_reference, CreditedOutput.output_index)
            )
        )
    state = RegistrationState(
        registration=registration,
        payment=payment,
        proofs=verified_proofs(session, registration),
        controllers=tuple(declared_controllers(registration)),
        identity=identity,
        outputs=outputs,
    )

    policy_failures: list[str] = []
    if registration.status == "under_review":
        policy_failures.append("registration is under review")
    if payment is not None:
        if ctx.registry.rail_state(payment.rail_id) is not RailState.ENABLED:
            policy_failures.append(f"payment option {payment.rail_id} is currently disabled")
        if state.payment_status is PaymentStatus.SETTLED:
            credited = sum(int(o.amount_base_units) for o in outputs)
            if credited < int(payment.amount_base_units):
                policy_failures.append("the settled payment is not fully credited")

    blockers = issuance_blockers(
        IssuanceInputs(
            payment_status=state.payment_status,
            controller_proof=state.proof_status,
            policy_checks_passed=not policy_failures,
            intent_valid=registration.status not in INVALID_REGISTRATION_STATUSES,
            intent_consumed=registration.consumed_at is not None or identity is not None,
        )
    )
    # Name the policy checks that failed instead of the generic sentence. Once the identity
    # exists nothing is blocking any more: asking again returns it.
    if identity is None:
        state.blockers = [
            _with_unproven(state, b)
            for b in blockers
            if not b.startswith("policy, risk or compliance")
        ] + policy_failures
    x402 = payment is not None and is_x402_rail(ctx.registry, payment.rail_id)
    state.next_action = _next_action(state, policy_failures, x402=x402)
    return state


def _with_unproven(state: RegistrationState, blocker: str) -> str:
    """With a key and a wallet, one of them proven: say which one is left."""
    if blocker.startswith("controller proof") and state.proofs and state.unproven:
        return f"{blocker} (still to prove: {', '.join(state.unproven)})"
    return blocker


def is_x402_rail(registry: PaymentRouteRegistry, rail_id: str) -> bool:
    rail = registry.rails.get(rail_id)
    return rail is not None and rail.protocol == "x402"


def _next_action(state: RegistrationState, policy_failures: list[str], *, x402: bool) -> str:
    if state.identity is not None:
        return "none"
    if state.registration.status in INVALID_REGISTRATION_STATUSES:
        return "request_new_quote"
    if x402 and state.payment_status is PaymentStatus.AWAITING_PAYMENT:
        # Paying comes last on x402, so nothing is charged for a DID that cannot be issued.
        if state.proof is None:
            return "prove_wallet_control"
        return "contact_support" if policy_failures else X402_PAY_ACTION
    if state.payment_status is not PaymentStatus.SETTLED:
        return NEXT_ACTIONS[state.payment_status]
    if state.proof is None:
        return "prove_wallet_control"
    if policy_failures:
        return "contact_support"
    return "register_did"


@dataclass(frozen=True)
class IssuedIdentity:
    identity: AgentIdentity
    credential: Credential
    entitlements: list[str]
    created: bool


def complete_registration(
    session: Session, ctx: IssuanceContext, registration_intent_id: str, now: datetime
) -> IssuedIdentity:
    # Lock order matches the payment worker: payment intent first, then the registration.
    payment_for(session, registration_intent_id, lock=True)
    registration = session.get(RegistrationIntent, registration_intent_id, with_for_update=True)
    if registration is None:
        raise ApiError(404, "not_found", "Unknown registration intent.")

    state = evaluate(session, ctx, registration)
    if state.identity is not None:
        return _existing(session, state.identity)
    if ctx.issuer is None:
        raise ApiError(
            503,
            "issuance_unavailable",
            "DID issuance is not enabled on this gateway yet.",
            headers={"Retry-After": "3600"},
        )
    if state.blockers:
        raise ApiError(
            409,
            "registration_not_ready",
            "The DID cannot be issued yet.",
            details={"blockers": state.blockers, "next_action": state.next_action},
        )
    return _issue(session, ctx, ctx.issuer, state, now)


def _issue(
    session: Session,
    ctx: IssuanceContext,
    issuer: Issuer,
    state: RegistrationState,
    now: datetime,
) -> IssuedIdentity:
    # Whole seconds, so issued_at in the API equals the credential's signed validFrom.
    now = now.replace(microsecond=0)
    registration, payment, proof = state.registration, state.payment, state.proof
    if payment is None or proof is None or proof.verified_at is None:  # excluded by the blockers
        raise RuntimeError("issuance reached without a settled payment and a verified proof")
    service = ctx.registry.services[registration.service_id]
    key_proof, wallet_proof = state.proofs.get(ED25519), state.proofs.get(EIP155)
    controller = (
        parse_controller_key(registration.controller_public_key)
        if registration.controller_public_key
        else None
    )
    wallet = (
        Wallet(registration.controller_network or "", registration.controller_wallet_address)
        if registration.controller_wallet_address
        else None
    )

    identity_id = new_id(IDENTITY)
    credential_id = new_id(CREDENTIAL)
    did = agent_did(ctx.public_base_url, identity_id)
    entitlements = [DID_HOLDER]
    received = sum(int(o.amount_base_units) for o in state.outputs)

    identity = AgentIdentity(
        id=identity_id,
        public_identifier=did,
        display_name=registration.display_name,
        participant_type=registration.participant_type,
        # Every controller here proved control (the issuance rule requires it).
        controller_public_key=controller.canonical if controller else None,
        controller_wallet_address=wallet.address if wallet else None,
        controller_network=wallet.network if wallet else None,
        registration_intent_id=registration.id,
        controller_proof_id=proof.id,
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(identity)
    session.flush()

    document = registration_credential(
        RegistrationFacts(
            credential_url=credential_url(ctx.public_base_url, credential_id),
            subject_did=did,
            display_name=registration.display_name,
            participant_type=registration.participant_type,
            controller=controller,
            controller_verified_at=key_proof.verified_at if key_proof else None,
            wallet=wallet,
            wallet_verified_at=wallet_proof.verified_at if wallet_proof else None,
            wallet_proof_method=wallet_proof.method if wallet_proof else None,
            registration_intent_id=registration.id,
            payment_intent_id=payment.id,
            service_title=service.title,
            commercial_currency=payment.commercial_price_currency,
            commercial_amount=f"{payment.commercial_price_amount:.2f}",
            payment_option=payment.rail_id,
            asset=payment.asset_symbol,
            network=payment.network,
            amount_paid=format_base_units(received, payment.asset_decimals),
            payment_outputs=[f"{o.transaction_reference}:{o.output_index}" for o in state.outputs],
            terms_version=registration.terms_version,
            entitlements=entitlements,
            issued_at=now,
        ),
        issuer,
    )
    credential = Credential(
        id=credential_id,
        identity_id=identity_id,
        registration_intent_id=registration.id,
        payment_intent_id=payment.id,
        format=CREDENTIAL_FORMAT,
        issuer_key_id=issuer.verification_method,
        policy_version=registration.terms_version,
        payload=document,
        signature=document["proof"]["proofValue"],
        issued_at=now,
    )
    session.add(credential)
    session.flush()
    for name in entitlements:
        session.add(
            Entitlement(
                subject_id=identity_id,
                entitlement=name,
                source_credential_id=credential_id,
                status="active",
                granted_at=now,
            )
        )

    registration.status = "issued"
    registration.consumed_at = now
    registration.updated_at = now

    common: dict[str, Any] = {"identity_id": identity_id, "did": did}
    record_audit_event(
        session,
        action="identity.issued",
        subject_type="registration_intent",
        subject_id=registration.id,
        actor_type="agent",
        data={
            **common,
            "payment_intent_id": payment.id,
            "payment_option": payment.rail_id,
            "controller_proof_id": proof.id,
            **({"wallet_proof_id": wallet_proof.id} if wallet_proof and controller else {}),
            "credential_id": credential_id,
            "terms_version": registration.terms_version,
        },
    )
    record_audit_event(
        session,
        action="credential.issued",
        subject_type="identity",
        subject_id=identity_id,
        actor_type="system",
        data={
            "credential_id": credential_id,
            "format": CREDENTIAL_FORMAT,
            "issuer_key_id": issuer.verification_method,
        },
    )
    record_audit_event(
        session,
        action="entitlement.granted",
        subject_type="identity",
        subject_id=identity_id,
        actor_type="system",
        data={"entitlements": entitlements, "source_credential_id": credential_id},
    )
    return IssuedIdentity(
        identity=identity, credential=credential, entitlements=entitlements, created=True
    )


def _existing(session: Session, identity: AgentIdentity) -> IssuedIdentity:
    credential = session.scalars(
        select(Credential)
        .where(Credential.identity_id == identity.id)
        .order_by(Credential.issued_at)
        .limit(1)
    ).one()
    return IssuedIdentity(
        identity=identity,
        credential=credential,
        entitlements=active_entitlements(session, identity.id),
        created=False,
    )


def active_entitlements(session: Session, identity_id: str) -> list[str]:
    return list(
        session.scalars(
            select(Entitlement.entitlement)
            .where(Entitlement.subject_id == identity_id, Entitlement.status == "active")
            .order_by(Entitlement.entitlement)
        )
    )


def credential_url(public_base_url: str, credential_id: str) -> str:
    return f"{public_base_url.rstrip('/')}/v1/credentials/{credential_id}"


def identity_record_url(public_base_url: str, identity_id: str) -> str:
    return f"{public_base_url.rstrip('/')}/v1/dids/{identity_id}"
