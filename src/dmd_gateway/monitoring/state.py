"""Small values the monitor and the payment worker keep between runs (``monitor_state``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from dmd_gateway.db.models import MonitorState

WORKER_HEARTBEAT = "heartbeat.worker"
AUDIT_CURSOR = "cursor.audit"
RECEIVING = "receiving.destinations"


def get_state(session: Session, key: str) -> tuple[dict[str, Any], datetime] | None:
    row = session.get(MonitorState, key)
    return (dict(row.value), row.updated_at) if row is not None else None


def put_state(session: Session, key: str, value: dict[str, Any], now: datetime) -> None:
    statement = insert(MonitorState).values(key=key, value=value, updated_at=now)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[MonitorState.key],
            set_={"value": statement.excluded.value, "updated_at": statement.excluded.updated_at},
        )
    )
