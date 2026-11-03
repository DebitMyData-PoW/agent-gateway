"""Suspending, reactivating and revoking an issued DID (ID-13).

An operator changes a DID's status with ``dmd-gateway-admin`` on the server; nothing on the
public API can. The change, who made it and why are written to the audit trail in the same
transaction, and the audit trail cannot be edited or deleted, so the history is kept.

  active     -> suspended   temporary: the DID stops resolving (410 Gone) until reactivated
  suspended  -> active      reactivated
  active     -> revoked     permanent: the DID's credentials and entitlements are revoked too
  suspended  -> revoked
  revoked    -> nothing     a revoked DID never comes back

The entitlements follow the DID (suspended with it, active again with it, revoked with it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dmd_gateway.audit import record_audit_event
from dmd_gateway.db.models import AgentIdentity, AuditEvent, Credential, Entitlement

STATUS_CHANGED = "identity.status_changed"
TARGETS = {"suspend": "suspended", "reactivate": "active", "revoke": "revoked"}
ALLOWED = {
    ("active", "suspended"),
    ("suspended", "active"),
    ("active", "revoked"),
    ("suspended", "revoked"),
}
REASON_LENGTH = (3, 300)
OPERATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,63}$")
ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
_IN_DID_OR_URL = re.compile(rf"agents[:/]({ULID})(?:/did\.json)?$")
_PUBLIC_DID = re.compile(rf"did:web:[^/?#\s]+:agents:({ULID})")
# The path parameter of the public records: an identity ID or a DID of this gateway.
IDENTITY_OR_DID = rf"^(dmd_identity_{ULID}|did:web:[^/?#\s]{{1,253}}:agents:{ULID})$"


class StatusChangeError(ValueError):
    """The change was refused; the message says why, in words for the operator."""


@dataclass(frozen=True)
class StatusChange:
    identity_id: str
    did: str
    display_name: str
    old: str
    new: str
    entitlements_changed: int
    credentials_revoked: int
    audit_event_id: int


def find_identity(session: Session, text: str, *, lock: bool = False) -> AgentIdentity:
    """The identity named by its DID, its did.json address, its identity ID, its bare ULID or
    its registration ID (dmd_did_intent_...)."""
    value = text.strip()
    identity_id = None
    if re.fullmatch(rf"dmd_identity_{ULID}", value):
        identity_id = value
    elif re.fullmatch(ULID, value):
        identity_id = f"dmd_identity_{value}"
    elif match := _IN_DID_OR_URL.search(value):
        identity_id = f"dmd_identity_{match.group(1)}"
    query = select(AgentIdentity)
    if identity_id is not None:
        query = query.where(AgentIdentity.id == identity_id)
    elif value.startswith("dmd_did_intent_"):
        query = query.where(AgentIdentity.registration_intent_id == value)
    else:
        query = query.where(AgentIdentity.public_identifier == value)
    if lock:
        query = query.with_for_update()
    identity = session.scalars(query).first()
    if identity is None:
        raise StatusChangeError(f"No DID found for {value!r} on this gateway.")
    return identity


def public_identity(session: Session, text: str, *, lock: bool = False) -> AgentIdentity | None:
    """The identity named exactly by its identity ID (dmd_identity_...) or by its DID as this
    gateway issued it (the ``:`` of a host's port may be written ``%3A`` or not), else None.

    For the public API and products: unlike ``find_identity`` (for operators), a DID that
    names another host is not this gateway's DID, even with the same ending."""
    value = text.strip()
    if re.fullmatch(rf"dmd_identity_{ULID}", value):
        identity_id = value
    elif match := _PUBLIC_DID.fullmatch(value):
        identity_id = f"dmd_identity_{match.group(1)}"
    else:
        return None
    identity = session.get(AgentIdentity, identity_id, with_for_update=lock)
    if identity is None:
        return None
    if value.startswith("did:") and unquote(identity.public_identifier) != unquote(value):
        return None
    return identity


def change_status(
    session: Session,
    text: str,
    command: str,
    *,
    reason: str,
    operator: str,
    now: datetime,
) -> StatusChange:
    """Apply suspend / reactivate / revoke to one DID, in the caller's transaction."""
    if command not in TARGETS:
        raise StatusChangeError(f"Unknown command {command!r}: use suspend, reactivate or revoke.")
    reason = " ".join(reason.split())
    low, high = REASON_LENGTH
    if not low <= len(reason) <= high:
        raise StatusChangeError(f"Give a reason of {low} to {high} characters.")
    if not OPERATOR.fullmatch(operator.strip()):
        raise StatusChangeError(
            "Give your name with --by (letters, digits, spaces, '.', '_', '@' or '-')."
        )
    operator = operator.strip()

    identity = find_identity(session, text, lock=True)
    old, new = identity.status, TARGETS[command]
    if old == new:
        raise StatusChangeError(f"This DID is already {old}: nothing was changed.")
    if (old, new) not in ALLOWED:
        raise StatusChangeError(f"A {old} DID cannot become {new}: nothing was changed.")

    identity.status = new
    identity.updated_at = now

    # The entitlements follow the DID. One revoked on its own never comes back.
    entitlements = session.scalars(
        select(Entitlement).where(
            Entitlement.subject_id == identity.id, Entitlement.status != "revoked"
        )
    ).all()
    for entitlement in entitlements:
        entitlement.status = new
        if new == "revoked":
            entitlement.revoked_at = now

    revoked_credentials = 0
    if new == "revoked":
        for credential in session.scalars(
            select(Credential).where(
                Credential.identity_id == identity.id, Credential.revoked_at.is_(None)
            )
        ):
            credential.revoked_at = now
            credential.revocation_reason = reason
            revoked_credentials += 1

    event = record_audit_event(
        session,
        action=STATUS_CHANGED,
        subject_type="identity",
        subject_id=identity.id,
        actor_type="operator",
        actor_id=operator,
        data={"did": identity.public_identifier, "from": old, "to": new, "reason": reason},
    )
    session.flush()
    return StatusChange(
        identity_id=identity.id,
        did=identity.public_identifier,
        display_name=identity.display_name,
        old=old,
        new=new,
        entitlements_changed=len(entitlements),
        credentials_revoked=revoked_credentials,
        audit_event_id=int(event.id),
    )


def history(session: Session, identity: AgentIdentity) -> list[AuditEvent]:
    """Every audit event about this DID, oldest first: its issuance, its credentials and every
    status change."""
    return list(
        session.scalars(
            select(AuditEvent)
            .where(
                or_(
                    (AuditEvent.subject_type == "identity")
                    & (AuditEvent.subject_id == identity.id),
                    (AuditEvent.subject_type == "registration_intent")
                    & (AuditEvent.subject_id == identity.registration_intent_id)
                    & (AuditEvent.action == "identity.issued"),
                )
            )
            .order_by(AuditEvent.id)
        )
    )
