"""The signed registration credential (ID-10, DEC-06).

One W3C Verifiable Credential (data model 2.0) per issued identity. It is also the signed
registration receipt: it names the payment, the controllers that proved control (Ed25519 key
and/or EVM wallet), the issuance time, the policy version and the credential ID, and it is
signed with the issuer key using the ``eddsa-jcs-2022`` Data Integrity cryptosuite.

``credentialSubject.controller`` is the controller that binds the identity: the public key
when there is one, otherwise the wallet. A wallet proven next to a key is in
``credentialSubject.wallet``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dmd_gateway.identity.crypto import ControllerKey, xml_datetime
from dmd_gateway.identity.issuer import Issuer
from dmd_gateway.identity.wallets import Wallet

VC_CONTEXT = ["https://www.w3.org/ns/credentials/v2"]
CREDENTIAL_TYPE = "DebitMyDataRegistrationCredential"
CREDENTIAL_FORMAT = "vc-2.0+eddsa-jcs-2022"


@dataclass(frozen=True)
class RegistrationFacts:
    credential_url: str
    subject_did: str
    display_name: str
    participant_type: str
    controller: ControllerKey | None
    controller_verified_at: datetime | None
    registration_intent_id: str
    payment_intent_id: str
    service_title: str
    commercial_currency: str
    commercial_amount: str
    payment_option: str
    asset: str
    network: str
    amount_paid: str
    payment_outputs: Sequence[str]
    terms_version: str
    entitlements: Sequence[str]
    issued_at: datetime
    wallet: Wallet | None = None
    wallet_verified_at: datetime | None = None
    wallet_proof_method: str | None = None


def _key(facts: RegistrationFacts) -> dict[str, Any] | None:
    if facts.controller is None or facts.controller_verified_at is None:
        return None
    return {
        "type": "Multikey",
        "publicKeyMultibase": facts.controller.multibase,
        "proofOfControl": "ed25519_challenge_signature",
        "verifiedAt": xml_datetime(facts.controller_verified_at),
    }


def _wallet(facts: RegistrationFacts) -> dict[str, Any] | None:
    if facts.wallet is None or facts.wallet_verified_at is None:
        return None
    return {
        "type": "BlockchainAccount",
        "blockchainAccountId": facts.wallet.account_id,
        # eip191_challenge_signature, erc1271_challenge_signature or erc6492_...
        "proofOfControl": (facts.wallet_proof_method or "eip191_signature").replace(
            "_signature", "_challenge_signature"
        ),
        "verifiedAt": xml_datetime(facts.wallet_verified_at),
    }


def registration_credential(facts: RegistrationFacts, issuer: Issuer) -> dict[str, Any]:
    key, wallet = _key(facts), _wallet(facts)
    controller = key or wallet
    if controller is None:
        raise ValueError("a registration credential needs a proven controller")
    document: dict[str, Any] = {
        "@context": VC_CONTEXT,
        "id": facts.credential_url,
        "type": ["VerifiableCredential", CREDENTIAL_TYPE],
        "issuer": {"id": issuer.did, "name": "DebitMyData"},
        "validFrom": xml_datetime(facts.issued_at),
        "credentialSubject": {
            "id": facts.subject_did,
            "displayName": facts.display_name,
            "participantType": facts.participant_type,
            "controller": controller,
            "registration": {
                "service": facts.service_title,
                "registrationIntentId": facts.registration_intent_id,
                "commercialPrice": {
                    "currency": facts.commercial_currency,
                    "amount": facts.commercial_amount,
                },
                "payment": {
                    "paymentIntentId": facts.payment_intent_id,
                    "paymentOption": facts.payment_option,
                    "asset": facts.asset,
                    "network": facts.network,
                    "amount": facts.amount_paid,
                    "outputs": list(facts.payment_outputs),
                    "status": "settled",
                },
                "termsVersion": facts.terms_version,
            },
            "entitlements": list(facts.entitlements),
        },
    }
    if key and wallet:
        document["credentialSubject"]["wallet"] = wallet
    return issuer.sign(document, created=facts.issued_at)


# --------------------------------------------------------------------------- receipts

RECEIPT_TYPE = "DebitMyDataDonationReceipt"


@dataclass(frozen=True)
class DonationFacts:
    receipt_url: str
    purchase_id: str
    purpose: str
    currency: str
    amount: str
    terms_version: str
    payment_intent_id: str
    payment_option: str
    asset: str
    network: str
    amount_paid: str
    payer_account: str  # CAIP-10, e.g. eip155:8453:0x...
    recipient: str
    transactions: Sequence[str]
    issued_at: datetime
    donor_name: str | None = None
    donor_did: str | None = None


def donation_receipt(facts: DonationFacts, issuer: Issuer) -> dict[str, Any]:
    """A donation receipt: a W3C Verifiable Credential signed like the DID credential.

    Its subject is the donor's DID when the donor gave one, otherwise the paying wallet as a
    ``did:pkh`` (CAIP-10) identifier, so anyone can check who paid what, and when.
    """
    donation: dict[str, Any] = {
        "purpose": facts.purpose,
        "purchaseId": facts.purchase_id,
        "amount": {"currency": facts.currency, "amount": facts.amount},
        "termsVersion": facts.terms_version,
    }
    if facts.donor_name:
        donation["donorName"] = facts.donor_name
    document: dict[str, Any] = {
        "@context": VC_CONTEXT,
        "id": facts.receipt_url,
        "type": ["VerifiableCredential", RECEIPT_TYPE],
        "issuer": {"id": issuer.did, "name": "DebitMyData"},
        "validFrom": xml_datetime(facts.issued_at),
        "credentialSubject": {
            "id": facts.donor_did or f"did:pkh:{facts.payer_account}",
            "donation": donation,
            "payment": {
                "paymentIntentId": facts.payment_intent_id,
                "paymentOption": facts.payment_option,
                "asset": facts.asset,
                "network": facts.network,
                "amount": facts.amount_paid,
                "payer": facts.payer_account,
                "recipient": facts.recipient,
                "transactions": list(facts.transactions),
                "status": "settled",
            },
        },
    }
    return issuer.sign(document, created=facts.issued_at)


PRODUCT_RECEIPT_TYPE = "DebitMyDataProductReceipt"


@dataclass(frozen=True)
class ProductFacts:
    receipt_url: str
    purchase_id: str
    product_id: str
    product_name: str
    entitlement: str
    did: str
    currency: str
    amount: str
    terms_version: str
    covers_from: datetime
    covers_until: datetime | None  # None: for good
    payment_intent_id: str
    payment_option: str
    asset: str
    network: str
    amount_paid: str
    recipient: str
    transactions: Sequence[str]
    issued_at: datetime


def product_receipt(facts: ProductFacts, issuer: Issuer) -> dict[str, Any]:
    """The receipt of a product bought for a DID (BottyBotty protection), signed like the DID
    credential. Its subject is the DID; ``validUntil`` is the end of the period it covers, so a
    service such as BottyBotty can check it with the issuer DID document alone."""
    product: dict[str, Any] = {
        "productId": facts.product_id,
        "name": facts.product_name,
        "purchaseId": facts.purchase_id,
        "entitlement": facts.entitlement,
        "price": {"currency": facts.currency, "amount": facts.amount},
        "termsVersion": facts.terms_version,
        "coversFrom": xml_datetime(facts.covers_from),
    }
    if facts.covers_until is not None:
        product["coversUntil"] = xml_datetime(facts.covers_until)
    document: dict[str, Any] = {
        "@context": VC_CONTEXT,
        "id": facts.receipt_url,
        "type": ["VerifiableCredential", PRODUCT_RECEIPT_TYPE],
        "issuer": {"id": issuer.did, "name": "DebitMyData"},
        "validFrom": xml_datetime(facts.issued_at),
        "credentialSubject": {
            "id": facts.did,
            "product": product,
            "payment": {
                "paymentIntentId": facts.payment_intent_id,
                "paymentOption": facts.payment_option,
                "asset": facts.asset,
                "network": facts.network,
                "amount": facts.amount_paid,
                "recipient": facts.recipient,
                "transactions": list(facts.transactions),
                "status": "settled",
            },
        },
    }
    if facts.covers_until is not None:
        document["validUntil"] = xml_datetime(facts.covers_until)
    return issuer.sign(document, created=facts.issued_at)
