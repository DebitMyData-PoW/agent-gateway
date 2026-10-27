"""Liveness and readiness probes (GW-08). They never expose configuration or error detail."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from dmd_gateway.signer.client import RemoteIssuer

router = APIRouter(tags=["health"])
log = logging.getLogger(__name__)


@router.get("/healthz", summary="Liveness probe")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
def readyz(request: Request) -> JSONResponse:
    checks = {"payment_route_registry": "ok", "database": "ok"}
    try:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        log.warning(
            "database readiness check failed", extra={"event": {"exc_type": type(exc).__name__}}
        )
        checks["database"] = "unavailable"

    # In production, the separate credential signer must answer with the expected key.
    issuer = request.app.state.issuance.issuer
    if isinstance(issuer, RemoteIssuer):
        checks["credential_signer"] = "ok" if issuer.ready() else "unavailable"

    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "unavailable", "checks": checks},
    )
