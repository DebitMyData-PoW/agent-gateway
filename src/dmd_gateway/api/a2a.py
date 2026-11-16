"""A2A, the Agent2Agent protocol (version 1.0, and 0.3 for earlier clients): the Agentic
Superhighway for agents that speak A2A rather than MCP.

    GET  /.well-known/agent-card.json   the Agent Card: who answers, where, and its skills
    POST /agenticsuperhighway/a2a       JSON-RPC 2.0: SendMessage (message/send in 0.3)

The skills are the MCP server's tools that need no sign-in (mcp_server.build_mcp_server), with
the same names, arguments and answers. An agent runs one by sending a message with a data part
``{"skill": "<tool name>", "arguments": {...}}``; the answer is a message whose data part is
the tool's result (and whose text part is the same, as JSON). This agent is not a language
model: a message without a skill gets the list of skills and how to call them.

Every answer comes at once, as a message: no task is kept (GetTask and CancelTask answer
TaskNotFoundError), and there is no streaming and no push notification. The version a client
speaks is its ``A2A-Version`` header (or query parameter): ``1.0``, or none for 0.3, as the
specification says. The answer takes that version's form: in 1.0 a part is ``{"text": ...}``
or ``{"data": ...}``, the role ``ROLE_AGENT`` and the result ``{"message": ...}``; in 0.3 a
part has a ``kind``, the role is ``agent`` and the result is the message itself. Either
method name is accepted (``SendMessage`` or ``message/send``).

Requests go through the same rate limits as the MCP server (nginx, the /agenticsuperhighway
location). Nothing here needs a sign-in: like the MCP tools, every step is paid for or signed
by the agent's own key or wallet.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from dmd_gateway import __version__
from dmd_gateway.api import brand, get_did_page
from dmd_gateway.api.server_card import (
    A2A_CARD_PATH,
    DESCRIPTION,
    TEST_DESCRIPTION,
    TITLE,
    WELL_KNOWN_CACHE,
    _document,
    _preflight,
    a2a_card_url,
)
from dmd_gateway.mcp_server import build_mcp_server, instructions
from dmd_gateway.settings import Settings

log = logging.getLogger("dmd_gateway.a2a")
router = APIRouter(include_in_schema=False)

CARD_PATH = A2A_CARD_PATH
A2A_PATH = f"{brand.MCP_PATH}/a2a"
VERSIONS = ("1.0", "0.3")
MAX_BODY = 64 * 1024  # as nginx allows
JSON_TYPES = ("application/json", "application/a2a+json")

# JSON-RPC 2.0 and A2A error codes (A2A specification, section 5.4).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
EXTENDED_CARD_NOT_CONFIGURED = -32007
VERSION_NOT_SUPPORTED = -32009

SEND = {"SendMessage", "message/send"}
STREAMING = {"SendStreamingMessage", "SubscribeToTask", "message/stream", "tasks/resubscribe"}
NO_TASKS = {"GetTask", "CancelTask", "tasks/get", "tasks/cancel"}
LIST_TASKS = {"ListTasks", "tasks/list"}
EXTENDED_CARD = {"GetExtendedAgentCard", "agent/getAuthenticatedExtendedCard"}
PUSH = {
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/set",
    "tasks/pushNotificationConfig/get",
    "tasks/pushNotificationConfig/list",
    "tasks/pushNotificationConfig/delete",
}

TAGS = ["did-llm", "identity", "did:web", "verifiable-credential", "payments", "debitmydata"]
FIRST_SKILL = "debitmydata_discover"


# --------------------------------------------------------------------------- the skills


def tools_server(app: FastAPI) -> MCPServer:
    """The MCP tools that need no sign-in, made once per app (they call its REST API)."""
    server: MCPServer | None = getattr(app.state, "a2a_tools", None)
    if server is None:
        server = build_mcp_server(app, sign_in=False)
        app.state.a2a_tools = server
    return server


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


async def skills(app: FastAPI) -> list[dict[str, Any]]:
    """The Agent Card's skills: one per tool, with how to call it as its example."""
    found = []
    for tool in await tools_server(app).list_tools():
        call = {"skill": tool.name, "arguments": _example_arguments(tool.input_schema)}
        found.append(
            {
                "id": tool.name,
                "name": tool.title or tool.name,
                "description": _one_line(tool.description),
                "tags": TAGS,
                "examples": [json.dumps(call)],
                "inputModes": ["application/json"],
                "outputModes": ["application/json", "text/plain"],
            }
        )
    return found


def _example_arguments(schema: dict[str, Any] | None) -> dict[str, str]:
    """The required arguments, each named by its type: what a call looks like."""
    schema = schema or {}
    properties = schema.get("properties") or {}
    return {
        name: f"<{(properties.get(name) or {}).get('type', 'value')}>"
        for name in schema.get("required") or []
    }


async def _skill_list(app: FastAPI) -> list[dict[str, Any]]:
    """For the help answer: every skill with its arguments (JSON Schema)."""
    return [
        {
            "skill": tool.name,
            "name": tool.title or tool.name,
            "description": _one_line(tool.description),
            "arguments": tool.input_schema,
        }
        for tool in await tools_server(app).list_tools()
    ]


# --------------------------------------------------------------------------- the Agent Card


def base_url(settings: Settings) -> str:
    return settings.public_base_url.rstrip("/")


def a2a_url(settings: Settings) -> str:
    return f"{base_url(settings)}{A2A_PATH}"


def card_url(settings: Settings) -> str:
    return a2a_card_url(settings)


async def agent_card(app: FastAPI) -> dict[str, Any]:
    settings: Settings = app.state.settings
    production = settings.environment == "production"
    url = a2a_url(settings)
    return {
        "name": TITLE if production else f"{TITLE} ({settings.environment})",
        "description": (
            f"{DESCRIPTION if production else TEST_DESCRIPTION} "
            f"{instructions(app).split(chr(10))[0]} "
            'Run a skill with a message holding a data part {"skill": "<id>", "arguments": '
            "{...}}; the answer is a message with the result as a data part. Start with "
            f"{FIRST_SKILL}."
        ),
        "version": __version__,
        "provider": {"organization": "DebitMyData", "url": get_did_page.ORGANIZATION_URL},
        "documentationUrl": f"{base_url(settings)}{brand.LLMS_PATH}",
        "iconUrl": get_did_page.product_image(settings),
        "supportedInterfaces": [
            {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": version}
            for version in VERSIONS
        ],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": await skills(app),
        # For 0.3 clients, which read these instead of supportedInterfaces (1.0 clients drop
        # them when supportedInterfaces is there).
        "url": url,
        "preferredTransport": "JSONRPC",
        "protocolVersion": "0.3.0",
    }


async def card(request: Request) -> Response:
    document = await agent_card(request.app)
    return _document(request, document, "application/json", WELL_KNOWN_CACHE)


router.add_api_route(CARD_PATH, card, methods=["GET", "HEAD"])
router.add_api_route(CARD_PATH, _preflight, methods=["OPTIONS"])


# --------------------------------------------------------------------------- JSON-RPC


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _version(request: Request) -> str:
    """The client's A2A version: its header, or its query parameter; none means 0.3."""
    given = request.headers.get("a2a-version")
    if given is None:
        given = request.query_params.get("A2A-Version", "")
    value = given.strip()
    if not value:
        return "0.3"
    major_minor = ".".join(value.split(".")[:2])
    if major_minor in VERSIONS:
        return major_minor
    raise RpcError(
        VERSION_NOT_SUPPORTED,
        f"A2A version {value!r} is not supported here",
        {"supportedVersions": list(VERSIONS)},
    )


def _reply(request_id: Any, *, result: Any = None, error: RpcError | None = None) -> Response:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        body["result"] = result
    else:
        body["error"] = {"code": error.code, "message": error.message}
        if error.data is not None:
            body["error"]["data"] = error.data
    return JSONResponse(body)


async def endpoint(request: Request) -> Response:
    request_id: Any = None
    try:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in JSON_TYPES:
            raise RpcError(INVALID_REQUEST, "Send JSON-RPC 2.0 as application/json")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise RpcError(INVALID_REQUEST, f"The request is larger than {MAX_BODY} bytes")
        try:
            call = json.loads(raw)
        except ValueError as exc:
            raise RpcError(PARSE_ERROR, "Parse error: the body is not JSON") from exc
        if not isinstance(call, dict):
            raise RpcError(INVALID_REQUEST, "One JSON-RPC request object (no batches)")
        request_id = call.get("id")
        if (
            call.get("jsonrpc") != "2.0"
            or not isinstance(call.get("method"), str)
            or not isinstance(request_id, str | int | type(None))
            or isinstance(request_id, bool)
        ):
            raise RpcError(INVALID_REQUEST, "Not a JSON-RPC 2.0 request")
        version = _version(request)
        result = await _dispatch(request.app, call["method"], call.get("params"), version)
    except RpcError as error:
        return _reply(request_id, error=error)
    return _reply(request_id, result=result)


router.add_api_route(A2A_PATH, endpoint, methods=["POST"])


async def _dispatch(app: FastAPI, method: str, params: Any, version: str) -> Any:
    if method in SEND:
        if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
            raise RpcError(INVALID_PARAMS, "params.message (a Message) is required")
        message = params["message"]
        if not isinstance(message.get("parts"), list) or not message["parts"]:
            raise RpcError(INVALID_PARAMS, "params.message.parts must be a non-empty list")
        answer = await _answer(app, message)
        return _message(answer, message, version)
    if method in STREAMING:
        raise RpcError(UNSUPPORTED_OPERATION, "This agent answers at once: no streaming")
    if method in NO_TASKS:
        raise RpcError(TASK_NOT_FOUND, "This agent keeps no tasks: every answer is a message")
    if method in LIST_TASKS:
        return {"tasks": []} if version == "1.0" else []
    if method in PUSH:
        raise RpcError(PUSH_NOT_SUPPORTED, "No push notifications: every answer is immediate")
    if method in EXTENDED_CARD:
        raise RpcError(EXTENDED_CARD_NOT_CONFIGURED, "There is no extended Agent Card")
    raise RpcError(METHOD_NOT_FOUND, f"Unknown method {method!r}")


# --------------------------------------------------------------------------- answering


def _skill_call(message: dict[str, Any]) -> dict[str, Any] | None:
    """The ``{"skill": ..., "arguments": ...}`` the message asks for: a data part (1.0
    ``{"data": ...}`` or 0.3 ``{"kind": "data", "data": ...}``), or a text part that is
    that JSON object."""
    for part in message["parts"]:
        if isinstance(part, dict) and isinstance(part.get("data"), dict):
            if isinstance(part["data"].get("skill"), str):
                return part["data"]
    for part in message["parts"]:
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str) and text.strip().startswith("{"):
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("skill"), str):
                return value
    return None


async def _answer(app: FastAPI, message: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The result to send back (as JSON) and its skill ("" for help)."""
    call = _skill_call(message)
    if call is None:
        return await _help(app), ""
    skill = call["skill"]
    names = {tool.name for tool in await tools_server(app).list_tools()}
    if skill not in names:
        help_ = await _help(app)
        error = {"code": "unknown_skill", "message": f"No skill named {skill!r} here."}
        return {"error": error, **help_}, skill
    arguments = call.get("arguments", {})
    if not isinstance(arguments, dict):
        error = {"code": "invalid_arguments", "message": "arguments must be a JSON object"}
        return {"error": error}, skill
    try:
        result = await tools_server(app).call_tool(skill, arguments)
    except ToolError as exc:
        log.info("a2a skill refused", extra={"event": {"skill": skill}})
        return _tool_error(str(exc), skill), skill
    except UnexpectedToolError:
        log.exception("a2a skill failed", extra={"event": {"skill": skill}})
        error = {"code": "internal_error", "message": "The skill failed; try again shortly."}
        return {"error": error}, skill
    dumped = result.model_dump(by_alias=True, exclude_none=True)
    payload = dumped.get("structuredContent")
    if payload is None:
        texts = [item.get("text", "") for item in dumped.get("content", [])]
        try:
            payload = json.loads("".join(texts))
        except ValueError:
            payload = {"text": "".join(texts)}
    if dumped.get("isError"):
        payload = {"error": payload}
    log.info("a2a skill", extra={"event": {"skill": skill, "error": bool(dumped.get("isError"))}})
    return payload if isinstance(payload, dict) else {"result": payload}, skill


def _tool_error(text: str, skill: str) -> dict[str, Any]:
    """A refused call: as the REST API refused it (its status, code and details), or the
    arguments the skill refused."""
    text = text.removeprefix(f"Error executing tool {skill}: ")
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return value if "error" in value else {"error": value}
    code = "invalid_arguments" if "validation error" in text else "invalid_request"
    return {"error": {"code": code, "message": text}}


async def _help(app: FastAPI) -> dict[str, Any]:
    settings: Settings = app.state.settings
    return {
        "about": instructions(app),
        "how_to_call": (
            'Send a message with a data part {"skill": "<skill>", "arguments": {...}} (or a '
            "text part that is this JSON). Each answer is a message whose data part is the "
            f"result. Start with {FIRST_SKILL}."
        ),
        "agent_card": card_url(settings),
        "mcp_server": get_did_page.mcp_url(settings),
        "skills": await _skill_list(app),
    }


def _message(
    answer: tuple[dict[str, Any], str], asked: dict[str, Any], version: str
) -> dict[str, Any]:
    payload, skill = answer
    context = asked.get("contextId")
    message: dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "contextId": context if isinstance(context, str) and context else str(uuid.uuid4()),
        "metadata": {"skill": skill or None, "error": "error" in payload},
    }
    text = json.dumps(payload, ensure_ascii=False)
    if version == "1.0":
        message["role"] = "ROLE_AGENT"
        message["parts"] = [
            {"text": text, "mediaType": "application/json"},
            {"data": payload, "mediaType": "application/json"},
        ]
        return {"message": message}
    message["kind"] = "message"
    message["role"] = "agent"
    message["parts"] = [
        {"kind": "text", "text": text},
        {"kind": "data", "data": payload},
    ]
    return message
