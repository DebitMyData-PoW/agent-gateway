"""Idempotency-Key handling for mutating requests (GW-06).

A caller that retries a request with the same ``Idempotency-Key`` gets the original response
back instead of creating a second quote. Reusing a key with a different request body is
rejected. The key row is written in the same transaction as the work it protects, so a
concurrent duplicate waits for the first request and then replays its result.

Keys should be random (for example a UUID): they are not tied to a caller identity.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dmd_gateway.db.models import IdempotencyKey
from dmd_gateway.errors import ApiError

KEY_PATTERN = r"^[A-Za-z0-9._:-]{16,128}$"


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay(row: IdempotencyKey, request_sha256: str) -> tuple[int, dict[str, Any]]:
    if row.request_sha256 != request_sha256:
        raise ApiError(
            422,
            "idempotency_key_reused",
            "This Idempotency-Key was already used with a different request.",
        )
    if row.response_body is None or row.response_status is None:
        raise ApiError(
            409, "request_in_progress", "A request with this Idempotency-Key is still running."
        )
    return row.response_status, row.response_body


def claim(
    session: Session, scope: str, key: str, request_sha256: str
) -> tuple[IdempotencyKey | None, tuple[int, dict[str, Any]] | None]:
    """Claim ``key`` for this request.

    Returns ``(row, None)`` when the key is new (the caller does the work and then calls
    ``complete``), or ``(None, (status, body))`` when an earlier response must be replayed.
    """
    existing = session.get(IdempotencyKey, (scope, key))
    if existing is not None:
        return None, _replay(existing, request_sha256)

    row = IdempotencyKey(scope=scope, key=key, request_sha256=request_sha256)
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # A concurrent request with the same key committed first.
        session.expunge_all()
        existing = session.get(IdempotencyKey, (scope, key))
        if existing is None:
            raise
        return None, _replay(existing, request_sha256)
    return row, None


def complete(row: IdempotencyKey, status: int, body: dict[str, Any], now: datetime) -> None:
    row.response_status = status
    row.response_body = body
    row.completed_at = now
