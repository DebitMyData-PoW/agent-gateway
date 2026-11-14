"""Signer messages: one JSON object per line, in each direction, one request per connection.

Requests:
    {"op": "describe"}
        -> {"ok": true, "did": "...", "public_key_multibase": "z6Mk..."}
    {"op": "sign", "document": {...}, "created": "2026-10-27T12:00:00Z"}
        -> {"ok": true, "document": {... with its proof ...}}
    {"op": "token", "assertion": "<the agent's signed request>", "resource": "https://...",
     "scope": "did:read", "ttl_seconds": 600}
        -> {"ok": true, "access_token": "...", "expires_in": 600, "scope": "did:read"}
    (describe also gives "token_key", the token key's public JWK, when the signer has one)
Any refusal or failure:
        -> {"ok": false, "error": "a short reason"}
"""

from __future__ import annotations

import json
import socket
from typing import Any

MAX_MESSAGE_BYTES = 1_048_576


class WireError(RuntimeError):
    pass


def encode(message: dict[str, Any]) -> bytes:
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise WireError("message too large")
    return data + b"\n"


def read_message(sock: socket.socket) -> dict[str, Any]:
    """Read one line (at most MAX_MESSAGE_BYTES) and parse it as a JSON object."""
    buffer = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buffer.extend(chunk)
        if b"\n" in chunk:
            break
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise WireError("message too large")
    line, newline, _ = bytes(buffer).partition(b"\n")
    if not newline or len(line) > MAX_MESSAGE_BYTES:
        raise WireError("incomplete or oversized message")
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireError("not a JSON message") from exc
    if not isinstance(value, dict):
        raise WireError("a message must be a JSON object")
    return value
