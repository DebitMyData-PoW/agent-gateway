"""What the signer agrees to sign, checked against the gateway's database (read-only).

The API builds the document; the signer checks that the facts it states are true before
signing it:

* a registration credential (``DebitMyDataRegistrationCredential``): the registration exists
  and is not already issued, the payment it names belongs to it and is settled, a controller
  proof for it is verified, the display name and participant type are the registration's,
  and no credential exists for it yet;
* a donation receipt (``DebitMyDataDonationReceipt``): the donation exists, is paid and has no
  receipt yet, and the payment it names belongs to it and is settled.

Anything else is refused. Both documents must name this issuer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from dmd_gateway.db.models import (
    ControllerChallenge,
    ControllerProof,
    Credential,
    PaymentIntent,
    Purchase,
    RegistrationIntent,
)
from dmd_gateway.domain.payment_status import PaymentStatus
from dmd_gateway.identity.credentials import CREDENTIAL_TYPE, RECEIPT_TYPE, VC_CONTEXT

SETTLED = PaymentStatus.SETTLED.value


class Refused(RuntimeError):
    """The document does not match the gateway's records; nothing is signed."""


def _field(value: Any, *path: str) -> Any:
    for name in path:
        if not isinstance(value, Mapping) or name not in value:
            raise Refused(f"the document has no {'.'.join(path)}")
        value = value[name]
    return value


def _text(value: Any, *path: str) -> str:
    result = _field(value, *path)
    if not isinstance(result, str) or not result:
        raise Refused(f"{'.'.join(path)} must be a non-empty string")
    return result


def kind_of(document: Mapping[str, Any], issuer_did: str) -> str:
    """``registration_credential`` or ``donation_receipt``, or refuse."""
    if document.get("@context") != VC_CONTEXT:
        raise Refused("unexpected @context")
    if "proof" in document:
        raise Refused("the document is already signed")
    if _text(document, "issuer", "id") != issuer_did:
        raise Refused("the document names another issuer")
    types = document.get("type")
    if types == ["VerifiableCredential", CREDENTIAL_TYPE]:
        return "registration_credential"
    if types == ["VerifiableCredential", RECEIPT_TYPE]:
        return "donation_receipt"
    raise Refused("only DID registration credentials and donation receipts are signed")


def check(session: Session, document: Mapping[str, Any], issuer_did: str) -> dict[str, str]:
    """Refuse unless the document's facts are true. Returns what it was checked against."""
    kind = kind_of(document, issuer_did)
    if kind == "registration_credential":
        return _check_registration(session, document)
    return _check_donation(session, document)


def _settled_payment(session: Session, payment_id: str) -> PaymentIntent:
    payment = session.get(PaymentIntent, payment_id)
    if payment is None:
        raise Refused("unknown payment")
    if payment.status != SETTLED:
        raise Refused(f"the payment is {payment.status}, not settled")
    return payment


def _check_registration(session: Session, document: Mapping[str, Any]) -> dict[str, str]:
    subject = _field(document, "credentialSubject")
    registration_id = _text(subject, "registration", "registrationIntentId")
    payment_id = _text(subject, "registration", "payment", "paymentIntentId")
    registration = session.get(RegistrationIntent, registration_id)
    if registration is None:
        raise Refused("unknown registration")
    if registration.status not in ("awaiting_payment", "paid"):
        raise Refused(f"the registration is {registration.status}")
    payment = _settled_payment(session, payment_id)
    if payment.registration_intent_id != registration.id:
        raise Refused("the payment belongs to another registration")
    if (
        _text(subject, "displayName") != registration.display_name
        or _text(subject, "participantType") != registration.participant_type
    ):
        raise Refused("the subject does not match the registration")
    proved = session.scalar(
        select(
            exists().where(
                ControllerProof.challenge_id == ControllerChallenge.id,
                ControllerChallenge.registration_intent_id == registration.id,
                ControllerProof.status == "verified",
            )
        )
    )
    if not proved:
        raise Refused("no verified controller proof for this registration")
    issued = session.scalar(
        select(exists().where(Credential.registration_intent_id == registration.id))
    )
    if issued:
        raise Refused("a credential already exists for this registration")
    return {
        "kind": "registration_credential",
        "registration": registration.id,
        "payment": payment.id,
    }


def _check_donation(session: Session, document: Mapping[str, Any]) -> dict[str, str]:
    subject = _field(document, "credentialSubject")
    purchase_id = _text(subject, "donation", "purchaseId")
    payment_id = _text(subject, "payment", "paymentIntentId")
    purchase = session.get(Purchase, purchase_id)
    if purchase is None:
        raise Refused("unknown donation")
    if purchase.status != "paid" or purchase.receipt is not None:
        raise Refused(f"the donation is {purchase.status}")
    payment = _settled_payment(session, payment_id)
    if payment.purchase_id != purchase.id:
        raise Refused("the payment belongs to another purchase")
    return {"kind": "donation_receipt", "purchase": purchase.id, "payment": payment.id}
