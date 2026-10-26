"""Error responses shared by every public endpoint.

Errors are returned as ``{"error": {"code": "...", "message": "...", ...details}}`` so that
agents can branch on a stable ``code`` instead of parsing messages. Internal details (stack
traces, database errors, configuration) are never included.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body_extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers
        # Top-level fields next to "error", for protocols that expect them (x402's 402 body).
        self.body_extra = body_extra or {}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **(details or {})}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={**exc.body_extra, **error_body(exc.code, exc.message, exc.details)},
            headers=exc.headers,
        )

    @app.exception_handler(SQLAlchemyError)
    async def _database_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        log.error("database error", extra={"event": {"exc_type": type(exc).__name__}})
        return JSONResponse(
            status_code=503,
            content=error_body("service_unavailable", "Temporarily unavailable; try again later."),
            headers={"Retry-After": "30"},
        )
