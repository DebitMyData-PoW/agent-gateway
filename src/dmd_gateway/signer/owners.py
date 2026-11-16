"""The owners a DID-LLM's signed records prove, which the signer checks before it signs an
access token or a sign-in grant for it (signer/tokens.py).

The database says who owns a DID-LLM now (``identities.controller_*``). The API writes it, so a
compromised API could write anyone's key there. The signer does not take the database's word
for it. It starts from the DID-LLM's registration credential, signed with DebitMyData's issuer
key (only the signer holds it, and signs a credential only for a new DID-LLM and the owners
that proved control: signer/policy.py), then follows each owner change applied since, in
order. Each one must be approved by an owner the DID-LLM had then and accepted by each new
owner, with the exact messages they signed (services/owner_changes.py). The owners at the end
must be the database's, and only the owners proven this way sign in through the signer.

The signer has no network, so it checks the signatures it can check alone: Ed25519 keys and
ordinary wallets (EIP-191). A smart wallet's signature cannot be checked here: a smart wallet
that became an owner is not proven (it cannot sign in through the signer anyway), and after a
change approved only by a smart wallet no owner is proven.

What a DID-LLM's owner revoked with a signed message stays refused, whatever its status says.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from dmd_gateway.db.models import (
    AgentIdentity,
    ControllerChallenge,
    ControllerProof,
    Credential,
    OwnerChange,
)
from dmd_gateway.identity.credentials import CREDENTIAL_TYPE
from dmd_gateway.identity.crypto import ControllerKeyError, parse_controller_key
from dmd_gateway.identity.issuer import IssuerKeys
from dmd_gateway.oauth import grants
from dmd_gateway.oauth.grants import GrantRefused
from dmd_gateway.services import owner_changes
from dmd_gateway.services.directory import controllers

ED25519, EIP155 = grants.ED25519, grants.EIP155


class OwnersRefused(RuntimeError):
    """The DID-LLM's records do not prove who owns it; nothing is signed for it."""


@dataclass(frozen=True)
class ProvenOwners:
    owners: dict[str, str]  # the owners the records name now: the database's
    proven: dict[str, str]  # those of them whose signatures prove they own it
    changed_at: datetime | None  # when the last owner change's messages were issued (signed)


def canonical_owner(kind: str, value: str) -> str:
    """An owner as the database stores it (``ed25519:z6Mk...``, ``eip155:8453:0x...``)."""
    if kind == ED25519:
        try:
            return parse_controller_key(value).canonical
        except ControllerKeyError as exc:
            raise OwnersRefused(f"an owner key cannot be read: {exc}") from exc
    return value


def credential_owners(subject: Any) -> dict[str, str]:
    """The owners a registration credential names (``credentialSubject``): the key, the
    wallet, or both."""
    found: dict[str, str] = {}
    if not isinstance(subject, Mapping):
        return found
    for entry in (subject.get("controller"), subject.get("wallet")):
        if not isinstance(entry, Mapping):
            continue
        key, account = entry.get("publicKeyMultibase"), entry.get("blockchainAccountId")
        if entry.get("type") == "Multikey" and isinstance(key, str) and ED25519 not in found:
            found[ED25519] = canonical_owner(ED25519, f"ed25519:{key}")
        elif (
            entry.get("type") == "BlockchainAccount"
            and isinstance(account, str)
            and EIP155 not in found
        ):
            found[EIP155] = account
    return found


def _registered_owners(
    session: Session, identity: AgentIdentity, keys: IssuerKeys
) -> dict[str, str]:
    """The owners named by the DID-LLM's registration credential, once its signature holds."""
    credential = session.scalars(
        select(Credential)
        .where(Credential.identity_id == identity.id)
        .order_by(Credential.issued_at, Credential.id)
        .limit(1)
    ).first()
    if credential is None:
        raise OwnersRefused("this DID-LLM has no registration credential")
    document = credential.payload if isinstance(credential.payload, dict) else {}
    issuer = document.get("issuer")
    subject = document.get("credentialSubject")
    if (
        document.get("type") != ["VerifiableCredential", CREDENTIAL_TYPE]
        or not isinstance(issuer, Mapping)
        or issuer.get("id") != keys.did
        or not isinstance(subject, Mapping)
        or subject.get("id") != identity.public_identifier
        or not keys.verifies(document)
    ):
        raise OwnersRefused(
            "this DID-LLM's registration credential is not one DebitMyData signed for it"
        )
    owners = credential_owners(subject)
    if not owners:
        raise OwnersRefused("this DID-LLM's registration credential names no owner")
    return owners


def _line(message: str, prefix: str) -> str:
    for line in message.split("\n"):
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    return ""


def _signed_by(
    challenge: ControllerChallenge,
    proof: ControllerProof,
    change: OwnerChange,
    identity: AgentIdentity,
    owners_now: dict[str, str],
) -> bool:
    """Whether this message is exactly the one the change asks this signer for, and the
    signature is the signer's, checked here with no network."""
    expected = owner_changes.change_message(
        audience=_line(challenge.message, "Audience: "),
        did=identity.public_identifier,
        change_id=change.id,
        signer=challenge.controller_value,
        signer_role=owner_changes.role(
            change, challenge.controller_type, challenge.controller_value
        ),
        owners_now=owners_now,
        new_owners=owner_changes.new_owners(change),
        nonce=challenge.nonce,
        issued_at=challenge.created_at,
        expires_at=challenge.expires_at,
    )
    if challenge.message != expected or proof.signature is None:
        return False
    try:
        return grants.owner_signed(
            challenge.controller_type, challenge.controller_value, expected, proof.signature
        )
    except GrantRefused:
        return False


def _revoked_by_owner(session: Session, identity: AgentIdentity) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    ControllerChallenge.identity_id == identity.id,
                    ControllerChallenge.purpose == owner_changes.REVOKE_PURPOSE,
                    ControllerProof.challenge_id == ControllerChallenge.id,
                    ControllerProof.status == "verified",
                )
            )
        )
    )


def proven_owners(session: Session, identity: AgentIdentity, keys: IssuerKeys) -> ProvenOwners:
    """The DID-LLM's owners, as its signed records prove them, or ``OwnersRefused``."""
    if _revoked_by_owner(session, identity):
        raise OwnersRefused("this DID-LLM was revoked by its owner")
    owners = _registered_owners(session, identity, keys)
    proven = dict(owners)  # DebitMyData's credential vouches for every owner it names
    changed_at: datetime | None = None
    changes = session.scalars(
        select(OwnerChange)
        .where(OwnerChange.identity_id == identity.id, OwnerChange.status == "applied")
        .order_by(OwnerChange.applied_at, OwnerChange.id)
    ).all()
    for change in changes:
        if owner_changes.previous_owners(change) != owners:
            raise OwnersRefused(
                f"owner change {change.id} does not start from the owners the DID-LLM had"
            )
        signed = session.execute(
            select(ControllerChallenge, ControllerProof)
            .join(ControllerProof, ControllerProof.challenge_id == ControllerChallenge.id)
            .where(
                ControllerChallenge.owner_change_id == change.id,
                ControllerChallenge.identity_id == identity.id,
                ControllerChallenge.purpose == owner_changes.CHANGE_PURPOSE,
                ControllerProof.status == "verified",
            )
            .order_by(ControllerChallenge.id)
        ).all()
        approved = False
        accepted: dict[str, str] = {}
        for challenge, proof in signed:
            kind, value = challenge.controller_type, challenge.controller_value
            if not _signed_by(challenge, proof, change, identity, owners):
                continue
            # The time the owners signed as the message's issue time (part of what they signed).
            if changed_at is None or challenge.created_at > changed_at:
                changed_at = challenge.created_at
            if owner_changes.role(change, kind, value) == owner_changes.APPROVE:
                approved = approved or proven.get(kind) == value
            else:
                accepted[kind] = value
        new = owner_changes.new_owners(change)
        # Who is proven after the change: an owner that stays keeps its proof, a new one
        # proves itself by accepting. Nobody is, if no proven owner's approval holds.
        proven = (
            {
                kind: value
                for kind, value in new.items()
                if proven.get(kind) == value or accepted.get(kind) == value
            }
            if approved
            else {}
        )
        owners = new
    if owners != controllers(identity):
        raise OwnersRefused(
            "the owners in the database are not the ones this DID-LLM's signed records name"
        )
    return ProvenOwners(owners=owners, proven=proven, changed_at=changed_at)
