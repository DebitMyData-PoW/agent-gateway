"""The signed registration credential (ID-10, DEC-06).

One W3C Verifiable Credential (data model 2.0) per issued identity. It is also the signed
registration receipt: it names the payment, the controller key that proved control, the
issuance time, the policy version and the credential ID, and it is signed with the issuer key
using the ``eddsa-jcs-2022`` Data Integrity cryptosuite.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dmd_gateway.identity.crypto import ControllerKey, add_proof, xml_datetime
from dmd_gateway.identity.issuer import IssuerKey

VC_CONTEXT = ["https://www.w3.org/ns/credentials/v2"]
CREDENTIAL_TYPE = "DebitMyDataRegistrationCredential"
CREDENTIAL_FORMAT = "vc-2.0+eddsa-jcs-2022"


@dataclass(frozen=True)
class RegistrationFacts:
    credential_url: str
    subject_did: str
    display_name: str
    participant_type: str
    controller: ControllerKey
    controller_verified_at: datetime
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


def registration_credential(facts: RegistrationFacts, issuer: IssuerKey) -> dict[str, Any]:
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
            "controller": {
                "type": "Multikey",
                "publicKeyMultibase": facts.controller.multibase,
                "proofOfControl": "ed25519_challenge_signature",
                "verifiedAt": xml_datetime(facts.controller_verified_at),
            },
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
    return add_proof(
        document,
        issuer.private_key,
        verification_method=issuer.verification_method,
        created=facts.issued_at,
    )
