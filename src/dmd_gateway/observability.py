"""Structured logging and request correlation IDs (GW-07).

Logs are single-line JSON. Request bodies, headers and secrets are never logged here.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "x-request-id"
_VALID_INCOMING_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            payload["correlation_id"] = correlation_id
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            payload.update(event)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # HTTP client libraries log every request at INFO, including node RPC calls.
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class CorrelationIdMiddleware:
    """Accepts a well-formed X-Request-ID from the caller or generates one, and echoes it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == CORRELATION_HEADER.encode():
                incoming = value.decode("latin-1")
                break
        correlation_id = (
            incoming if incoming and _VALID_INCOMING_ID.match(incoming) else uuid.uuid4().hex
        )
        token = _correlation_id.set(correlation_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((CORRELATION_HEADER.encode(), correlation_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _correlation_id.reset(token)
