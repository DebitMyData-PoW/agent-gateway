"""Staging docs page: request bodies as one input field per value.

Swagger UI shows a JSON request body as a text box to edit by hand, and a form body as one
input field per value (a list to pick from where the values are fixed). On servers with the
staging test tools, the API docs therefore list a form body first for every JSON request
body under /v1, and ``FormBodyMiddleware`` turns such a form into the same JSON before the
endpoint sees it, so the checks and the answers are exactly those of a JSON request.

Agents keep sending JSON. Production never accepts forms: both parts are installed only
when the staging test tools are on, and those are refused in production.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

FORM = "application/x-www-form-urlencoded"
JSON = "application/json"
PREFIX = "/v1/"
MAX_FORM_BYTES = 16 * 1024
MAX_FIELDS = 32
_SIMPLE_TYPES = {"string", "integer", "number", "boolean"}
_KEPT = ("type", "description", "pattern", "minLength", "maxLength", "enum", "default", "format")


def _content_type(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"content-type":
            return value.decode("latin-1").split(";", 1)[0].strip().lower()
    return ""


class FormBodyMiddleware:
    """Turns a form-encoded POST body under /v1 into the equivalent JSON object.

    Empty fields are left out, as if they had not been filled in.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].startswith(PREFIX)
            or _content_type(scope) != FORM
        ):
            await self.app(scope, receive, send)
            return

        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > MAX_FORM_BYTES:
                await _error(413, "form_too_large", "The form is too large.")(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        try:
            pairs = parse_qsl(
                body.decode("utf-8"), keep_blank_values=True, max_num_fields=MAX_FIELDS
            )
        except (UnicodeDecodeError, ValueError):
            await _error(400, "invalid_form", "The form could not be read.")(scope, receive, send)
            return
        data: dict[str, str] = {}
        for key, value in pairs:
            if key in data:
                await _error(422, "invalid_form", f"The field {key} is given more than once.")(
                    scope, receive, send
                )
                return
            if value.strip():
                data[key] = value

        converted = json.dumps(data).encode("utf-8")
        headers = [
            (name, value)
            for name, value in scope["headers"]
            if name not in (b"content-type", b"content-length")
        ]
        headers += [
            (b"content-type", JSON.encode()),
            (b"content-length", str(len(converted)).encode()),
        ]
        delivered = False

        async def receive_converted() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": converted, "more_body": False}
            return await receive()

        await self.app({**scope, "headers": headers}, receive_converted, send)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# --------------------------------------------------------------------------- API docs


@dataclass(frozen=True)
class FormHints:
    """How the docs page forms differ from the plain JSON fields.

    ``choices`` turns the named fields into a list to pick from (for example the enabled
    payment options), ``defaults`` pre-fills fields, ``required`` marks fields the page must
    not send empty, and ``hidden`` fields are left out of the forms.
    """

    choices: Mapping[str, list[str]] = field(default_factory=dict)
    defaults: Mapping[str, str] = field(default_factory=dict)
    required: Collection[str] = ()
    hidden: Collection[str] = ()


def add_form_bodies(schema: dict[str, Any], hints: FormHints) -> None:
    """List a form body first for every JSON request body under /v1.

    Fields that are objects or lists (such as ``metadata``) are left out of the forms; JSON
    can still send every field.
    """
    components = schema.get("components", {}).get("schemas", {})
    for path, item in schema.get("paths", {}).items():
        if not path.startswith(PREFIX):
            continue
        for operation in item.values():
            body = operation.get("requestBody") if isinstance(operation, dict) else None
            media = (body or {}).get("content", {}).get(JSON)
            if not media:
                continue
            form = _form_schema(media.get("schema", {}), components, hints)
            if form is not None:
                body["content"] = {FORM: {"schema": form}, **body["content"]}


def _resolve(node: Mapping[str, Any], components: Mapping[str, Any]) -> Mapping[str, Any]:
    """Follows a $ref and unwraps "X or null" (optional values), keeping the description and
    default given next to them."""
    own = {key: node[key] for key in ("description", "default") if node.get(key) is not None}
    if "$ref" in node:
        return {**_resolve(components[node["$ref"].rsplit("/", 1)[-1]], components), **own}
    options = [o for o in node.get("anyOf", []) if o.get("type") != "null"]
    if len(options) == 1 and len(node["anyOf"]) == 2:
        return {**_resolve(options[0], components), **own}
    return node


def _form_schema(
    node: Mapping[str, Any], components: Mapping[str, Any], hints: FormHints
) -> dict[str, Any] | None:
    model = _resolve(node, components)
    properties: dict[str, Any] = {}
    for name, raw in model.get("properties", {}).items():
        prop = _resolve(raw, components)
        if name in hints.hidden or prop.get("type") not in _SIMPLE_TYPES:
            continue
        field_schema = {key: copy.deepcopy(prop[key]) for key in _KEPT if key in prop}
        if hints.choices.get(name):
            field_schema["enum"] = list(hints.choices[name])
        if name in hints.defaults:
            field_schema["default"] = hints.defaults[name]
        if "default" not in field_schema and "enum" not in field_schema:
            # Starts empty. Otherwise Swagger UI fills the field with a made-up value that
            # matches its pattern, such as an ID that looks real but belongs to nothing.
            field_schema["example"] = ""
        properties[name] = field_schema
    if not properties:
        return None
    form: dict[str, Any] = {"type": "object", "properties": properties}
    required = [
        name for name in properties if name in model.get("required", []) or name in hints.required
    ]
    if required:
        form["required"] = required
    return form
