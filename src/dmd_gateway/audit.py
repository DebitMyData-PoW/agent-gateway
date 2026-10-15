"""Write-once audit trail (GW-05).

Rows are only ever inserted; the database rejects UPDATE/DELETE/TRUNCATE on the table.
Never put secrets, private keys, seed phrases or full signatures in ``data``.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.orm import Session

from dmd_gateway.db.models import AuditEvent
from dmd_gateway.observability import get_correlation_id

ActorType = Literal["system", "worker", "agent", "operator"]


def record_audit_event(
    session: Session,
    *,
    action: str,
    subject_type: str,
    subject_id: str,
    actor_type: ActorType,
    actor_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    """Add an audit event to the caller's transaction (committed together with the change)."""
    event = AuditEvent(
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=get_correlation_id(),
        data=data or {},
    )
    session.add(event)
    return event
