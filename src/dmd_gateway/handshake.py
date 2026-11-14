"""dmd-gateway-handshake: the discovery runbook's Day 5 acceptance check ("Handshake
Verification"). An automated client does, unsupervised, what any agent does to find and use
the Agentic Superhighway, and says what fails.

    dmd-gateway-handshake https://api.debitmydata.io           from anywhere, as agents do
    dmd-gateway-handshake --local https://api.debitmydata.io   on the server itself (updates)
    dmd-gateway-handshake --registry https://api.debitmydata.io   also the MCP Registry
    dmd-gateway-handshake --json ...                            the results as JSON

1. Discovery: ``/.well-known/mcp`` and ``/.well-known/mcp/server-card.json`` answer 200 in
   ``application/json`` with the runbook's fields; the standard Server Card and the AI Catalog
   point to the same MCP address; ``llms.txt`` names it.
2. The MCP handshake: ``initialize`` (protocol version, capabilities, server name and
   version), ``notifications/initialized``, ``tools/list`` (every tool and every parameter
   described in plain words, with a JSON Schema), and JSON-RPC errors as the specification
   says (-32700 parse error, -32601 method not found, -32602 invalid params; a wrong tool
   argument is named in the tool's error).
3. Sign-in negotiation, when the discovery files offer ``oauth2.1``: a tool that needs a
   signed-in agent, called without a token, answers HTTP 401 with a ``WWW-Authenticate``
   challenge (``resource_metadata``, ``scope``); the protected resource metadata names exactly
   this MCP address and its sign-in server; that server's metadata (RFC 8414) and keys answer;
   a wrong token is refused (401 ``invalid_token``); with ``wallet-signature``, the wallet's
   challenge and token requests answer as OAuth does (an unknown DID-LLM refused, 400).
4. One truth: the tools in the runbook's card are exactly those ``tools/list`` gives, and the
   manifest, both cards and ``initialize`` give the same version; with ``--registry``, the
   official MCP Registry lists the server with the same address, title and description.

``--local`` sends every request to this server (127.0.0.1:443) under the public names, as the
update scripts check nginx: the certificate is Cloudflare's origin certificate, which only
Cloudflare trusts, so it is not checked there. The exit status is 0 when nothing fails
(warnings and skipped steps aside), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp_types.version import LATEST_PROTOCOL_VERSION

from dmd_gateway.oauth.wallet_sign_in import GRANT_TYPE as WALLET_GRANT

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
JSON_TYPE = "application/json"
CARD_TYPE = "application/mcp-server-card+json"
MCP_ACCEPT = "application/json, text/event-stream"
REGISTRY = "https://registry.modelcontextprotocol.io"
TIMEOUT_SECONDS = 15
CLIENT_INFO = {"name": "dmd-gateway-handshake", "version": "1"}
NOT_A_TOKEN = "not-a-token"  # noqa: S105 - what a wrong token looks like
_CHALLENGE_FIELD = re.compile(r'(\w+)="([^"]*)"')


class Failed(Exception):
    """A step does not hold up: ``detail`` says what was expected and what came."""


class Warned(Exception):
    """A step works, with something to look at (a warning, not a failure)."""


@dataclass
class Result:
    step: str
    status: str
    detail: str = ""


@dataclass
class Report:
    url: str
    results: list[Result] = field(default_factory=list)

    @property
    def failed(self) -> list[Result]:
        return [result for result in self.results if result.status == FAIL]

    @property
    def warnings(self) -> list[Result]:
        return [result for result in self.results if result.status == WARN]

    def summary(self) -> str:
        passed = sum(result.status == PASS for result in self.results)
        if self.failed:
            return f"{len(self.failed)} of {len(self.results)} checks failed"
        extra = f", {len(self.warnings)} warning(s)" if self.warnings else ""
        return f"handshake complete: {passed} checks passed{extra}"

    def text(self) -> str:
        width = max((len(result.step) for result in self.results), default=0)
        lines = [f"Handshake with {self.url}"]
        for result in self.results:
            detail = f"  {result.detail}" if result.detail else ""
            lines.append(f"{result.status.upper():4}  {result.step:<{width}}{detail}")
        lines.append(self.summary())
        return "\n".join(lines)


def _content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip().lower()


def _json(response: httpx.Response, *types: str) -> Any:
    """The JSON of a 200 answer in one of ``types`` (application/json by default)."""
    expected = types or (JSON_TYPE,)
    if response.status_code != 200:
        raise Failed(f"HTTP {response.status_code} at {response.url}")
    if _content_type(response) not in expected:
        raise Failed(f"{response.url} is {_content_type(response) or 'untyped'}, not {expected[0]}")
    try:
        return response.json()
    except ValueError as exc:
        raise Failed(f"{response.url} is not JSON") from exc


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Failed(f"{what} is not a JSON object")
    return value


def _text_field(document: dict[str, Any], name: str, what: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Failed(f"{what} has no {name}")
    return value


def _web_address(url: str) -> bool:
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1")
    return bool(parts.hostname) and (parts.scheme == "https" or (local and parts.scheme == "http"))


def rpc_message(response: httpx.Response, request_id: Any) -> dict[str, Any]:
    """The JSON-RPC answer to ``request_id``, sent as JSON or as server-sent events."""
    kind = _content_type(response)
    messages: list[Any] = []
    if kind == JSON_TYPE:
        try:
            body = response.json()
        except ValueError as exc:
            raise Failed("the MCP answer is not JSON") from exc
        messages = body if isinstance(body, list) else [body]
    elif kind == "text/event-stream":
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    messages.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
    else:
        raise Failed(f"the MCP answer is {kind or 'untyped'} (HTTP {response.status_code})")
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise Failed(f"no JSON-RPC answer to request {request_id!r} (HTTP {response.status_code})")


def bearer_challenge(header: str) -> dict[str, str]:
    """The fields of a ``WWW-Authenticate: Bearer ...`` challenge."""
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        raise Failed(f"the challenge is not Bearer: {header[:60]}")
    return dict(_CHALLENGE_FIELD.findall(rest))


class Handshake:
    """The checks, in order; each later step uses what the earlier ones found."""

    def __init__(
        self,
        url: str,
        http: httpx.Client,
        *,
        registry: httpx.Client | None = None,
        registry_url: str = REGISTRY,
    ) -> None:
        self.base = url.rstrip("/")
        self.http = http
        self.registry = registry
        self.registry_url = registry_url.rstrip("/")
        self.report = Report(self.base)
        self.endpoint = ""
        self.card_url = ""
        self.version = ""
        self.initialized_version = ""
        self.authentication: list[str] = []
        self.card_tools: list[dict[str, Any]] = []
        self.listed: list[dict[str, Any]] = []
        self.server_card: dict[str, Any] = {}
        self.session: str | None = None
        self.protocol: str | None = None
        self._ids = 0

    # ----------------------------------------------------------------------- running

    def step(self, name: str, check: Callable[[], str]) -> bool:
        try:
            detail = check()
        except Failed as exc:
            self.report.results.append(Result(name, FAIL, str(exc)))
            return False
        except Warned as exc:
            self.report.results.append(Result(name, WARN, str(exc)))
            return True
        except httpx.HTTPError as exc:
            self.report.results.append(Result(name, FAIL, f"no answer: {type(exc).__name__}"))
            return False
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            detail = f"unexpected answer ({type(exc).__name__}: {exc})"
            self.report.results.append(Result(name, FAIL, detail))
            return False
        self.report.results.append(Result(name, PASS, detail))
        return True

    def skip(self, name: str, why: str) -> None:
        self.report.results.append(Result(name, SKIP, why))

    def run(self) -> Report:
        if not self.step("discovery: /.well-known/mcp", self.manifest):
            return self.report  # without the MCP address, nothing else can be found
        self.step("discovery: /.well-known/mcp/server-card.json", self.runbook_card)
        self.step("discovery: Server Card", self.standard_card)
        self.step("discovery: AI Catalog", self.catalog)
        self.step("discovery: llms.txt", self.llms)
        if not self.step("mcp: initialize", self.initialize):
            return self.report
        self.step("mcp: notifications/initialized", self.initialized)
        if self.step("mcp: tools/list", self.tools):
            self.step("one truth: the same tools everywhere", self.same_tools)
        self.step("one truth: the same version everywhere", self.same_version)
        self.step("json-rpc: -32700 parse error", self.parse_error)
        self.step("json-rpc: -32601 method not found", self.unknown_method)
        self.step("json-rpc: -32602 invalid params", self.invalid_params)
        self.step("json-rpc: a wrong argument is named", self.wrong_argument)
        if "oauth2.1" in self.authentication:
            self.sign_in()
        else:
            self.skip("sign-in (OAuth 2.1)", "not offered here: the discovery files list none")
        if self.registry is not None:
            self.step("MCP Registry: listed as the card says", self.registry_listing)
        return self.report

    # ----------------------------------------------------------------------- discovery

    def get(self, url: str, accept: str = JSON_TYPE) -> httpx.Response:
        return self.http.get(url, headers={"Accept": accept})

    def manifest(self) -> str:
        document = _object(_json(self.get(f"{self.base}/.well-known/mcp")), "the manifest")
        _text_field(document, "mcp_version", "the manifest")
        endpoints = document.get("endpoints")
        if not isinstance(endpoints, list) or not endpoints:
            raise Failed("the manifest lists no endpoint")
        endpoint = _object(endpoints[0], "the manifest's endpoint")
        self.endpoint = _text_field(endpoint, "url", "the manifest's endpoint")
        if not _web_address(self.endpoint):
            raise Failed(f"the MCP address is not https: {self.endpoint}")
        if endpoint.get("transport") != "streamable-http":
            raise Failed(f"the transport is {endpoint.get('transport')!r}, not streamable-http")
        authentication = endpoint.get("authentication", [])
        self.authentication = [str(item) for item in authentication or []]
        self.card_url = _text_field(document, "server_card", "the manifest")
        self.version = _text_field(document, "server_version", "the manifest")
        offered = ", ".join(self.authentication) or "none (open to all)"
        return f"MCP at {self.endpoint}; authentication: {offered}"

    def runbook_card(self) -> str:
        url = f"{self.base}/.well-known/mcp/server-card.json"
        document = _object(_json(self.get(url)), "the runbook's card")
        _text_field(document, "mcp_version", "the runbook's card")
        info = _object(document.get("server_info"), "the card's server_info")
        _text_field(info, "name", "the card's server_info")
        if info.get("version") != self.version:
            raise Failed(f"version {info.get('version')} here, {self.version} in the manifest")
        endpoints = document.get("endpoints") or [{}]
        if _object(endpoints[0], "the card's endpoint").get("url") != self.endpoint:
            raise Failed("the card names another MCP address than the manifest")
        tools = document.get("tools")
        if not isinstance(tools, list) or not tools:
            raise Failed("the card lists no tools")
        for tool in tools:
            _text_field(_object(tool, "a tool"), "name", "a tool of the card")
            _text_field(tool, "description", f"the card's tool {tool['name']}")
        self.card_tools = tools
        return f"{len(tools)} tools"

    def standard_card(self) -> str:
        card = _object(_json(self.get(self.card_url, CARD_TYPE), CARD_TYPE, JSON_TYPE), "card")
        _text_field(card, "name", "the Server Card")
        remotes = card.get("remotes") or [{}]
        if _object(remotes[0], "the card's remote").get("url") != self.endpoint:
            raise Failed("the Server Card names another MCP address")
        if card.get("version") != self.version:
            raise Failed(f"version {card.get('version')} here, {self.version} in the manifest")
        self.server_card = card
        return str(card["name"])

    def catalog(self) -> str:
        response = self.get(f"{self.base}/.well-known/ai-catalog.json")
        document = _json(response, JSON_TYPE, "application/ai-catalog+json")
        entries = _object(document, "the AI Catalog").get("entries") or []
        urls = [entry.get("url") for entry in entries if isinstance(entry, dict)]
        if self.card_url not in urls:
            raise Failed("the AI Catalog does not point to the Server Card")
        return "points to the Server Card"

    def llms(self) -> str:
        response = self.get(f"{self.base}/llms.txt", "text/plain, text/markdown")
        if response.status_code != 200:
            raise Failed(f"HTTP {response.status_code}")
        if self.endpoint not in response.text:
            raise Failed("llms.txt does not give the MCP address")
        return "gives the MCP address"

    # ----------------------------------------------------------------------- MCP

    def post(
        self, body: Any, *, raw: bytes | None = None, token: str | None = None
    ) -> httpx.Response:
        headers = {"Accept": MCP_ACCEPT, "Content-Type": JSON_TYPE}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        content = raw if raw is not None else json.dumps(body).encode("utf-8")
        return self.http.post(self.endpoint, content=content, headers=headers)

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, token: str | None = None
    ) -> tuple[httpx.Response, dict[str, Any]]:
        self._ids += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._ids, "method": method}
        if params is not None:
            body["params"] = params
        response = self.post(body, token=token)
        if response.status_code in (401, 403):
            return response, {}
        return response, rpc_message(response, self._ids)

    @staticmethod
    def result(answer: dict[str, Any]) -> dict[str, Any]:
        if "error" in answer:
            error = _object(answer["error"], "the error")
            raise Failed(f"JSON-RPC error {error.get('code')}: {error.get('message')}")
        return _object(answer.get("result"), "the result")

    def initialize(self) -> str:
        params = {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        response, answer = self.call("initialize", params)
        if response.status_code in (401, 403):
            raise Failed(f"HTTP {response.status_code}: initialize must be open to all")
        result = self.result(answer)
        protocol = _text_field(result, "protocolVersion", "initialize")
        capabilities = _object(result.get("capabilities"), "the capabilities")
        if "tools" not in capabilities:
            raise Failed("the capabilities offer no tools")
        info = _object(result.get("serverInfo"), "serverInfo")
        name = _text_field(info, "name", "serverInfo")
        self.initialized_version = str(info.get("version", ""))
        self.session = response.headers.get("mcp-session-id")
        self.protocol = protocol
        return f"protocol {protocol}, {name} {self.initialized_version}"

    def initialized(self) -> str:
        response = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if response.status_code not in (200, 202, 204):
            raise Failed(f"HTTP {response.status_code}")
        return f"HTTP {response.status_code}"

    def tools(self) -> str:
        _, answer = self.call("tools/list", {})
        tools = self.result(answer).get("tools")
        if not isinstance(tools, list) or not tools:
            raise Failed("no tools")
        undescribed: list[str] = []
        for tool in tools:
            name = _text_field(_object(tool, "a tool"), "name", "a tool")
            _text_field(tool, "description", f"the tool {name}")
            schema = _object(tool.get("inputSchema"), f"the input schema of {name}")
            if schema.get("type") != "object":
                raise Failed(f"the input schema of {name} is not an object schema")
            for parameter, spec in (schema.get("properties") or {}).items():
                if not isinstance(spec, dict) or not str(spec.get("description", "")).strip():
                    undescribed.append(f"{name}.{parameter}")
        if undescribed:
            raise Failed("parameters without a description: " + ", ".join(undescribed))
        self.listed = tools
        return f"{len(tools)} tools, each parameter described"

    def same_tools(self) -> str:
        if not self.card_tools:
            raise Failed("the runbook's card gave no tools to compare")
        card = {tool["name"]: tool.get("description") for tool in self.card_tools}
        live = {tool["name"]: tool.get("description") for tool in self.listed}
        if set(card) != set(live):
            only_card = ", ".join(sorted(set(card) - set(live))) or "none"
            only_live = ", ".join(sorted(set(live) - set(card))) or "none"
            raise Failed(f"only in the card: {only_card}; only in tools/list: {only_live}")
        changed = sorted(name for name in card if card[name] != live[name])
        if changed:
            raise Failed("described differently: " + ", ".join(changed))
        return "the runbook's card and tools/list agree"

    def same_version(self) -> str:
        found = {"manifest": self.version, "initialize": self.initialized_version}
        if self.server_card:
            found["Server Card"] = str(self.server_card.get("version"))
        if len(set(found.values())) != 1:
            raise Failed(", ".join(f"{where}: {version}" for where, version in found.items()))
        return f"version {self.version}"

    def _error_code(self, response: httpx.Response, request_id: Any) -> int | None:
        answer = rpc_message(response, request_id)
        error = answer.get("error")
        return error.get("code") if isinstance(error, dict) else None

    def parse_error(self) -> str:
        response = self.post(None, raw=b'{"jsonrpc": "2.0", "id": 1, "method": ')
        code = self._error_code(response, None)
        if code != -32700:
            raise Failed(f"error code {code}, HTTP {response.status_code}")
        return f"HTTP {response.status_code}"

    def unknown_method(self) -> str:
        _, answer = self.call("dmd/no-such-method", {})
        code = (answer.get("error") or {}).get("code")
        if code != -32601:
            raise Failed(f"error code {code}")
        return "answered"

    def invalid_params(self) -> str:
        _, answer = self.call("tools/call", {"name": 5, "arguments": "none"})
        code = (answer.get("error") or {}).get("code")
        if code != -32602:
            raise Failed(f"error code {code}")
        return "answered"

    def wrong_argument(self) -> str:
        # A wrong argument is refused before the tool runs; read-only tools first anyway.
        def order(tool: dict[str, Any]) -> tuple[bool, bool]:
            read_only = (tool.get("annotations") or {}).get("readOnlyHint") is True
            return (not read_only, "_get_" not in f"_{tool.get('name', '')}")

        for tool in sorted(self.listed, key=order):
            schema = tool.get("inputSchema") or {}
            properties = schema.get("properties") or {}
            for name in schema.get("required") or []:
                if (properties.get(name) or {}).get("type") == "string":
                    _, answer = self.call(
                        "tools/call", {"name": tool["name"], "arguments": {name: 12345}}
                    )
                    if "error" in answer:
                        code = answer["error"].get("code")
                        message = str(answer["error"].get("message", ""))
                        if code == -32602 and name in message:
                            return f"{tool['name']}: -32602 naming {name}"
                        raise Failed(f"error {code} that does not name {name}")
                    result = self.result(answer)
                    text = json.dumps(result.get("content", []))
                    if result.get("isError") is True and name in text:
                        return f"{tool['name']}: a tool error naming {name}"
                    raise Failed(f"{tool['name']} accepted a number for {name}")
        raise Warned("no tool with a required text parameter to try")

    # ----------------------------------------------------------------------- sign-in

    def sign_in(self) -> None:
        protected = [
            tool["name"]
            for tool in self.listed
            if any(
                isinstance(scheme, dict) and scheme.get("type") == "oauth2"
                for scheme in ((tool.get("_meta") or {}).get("securitySchemes") or [])
            )
        ]
        state: dict[str, Any] = {}

        def challenge() -> str:
            if not protected:
                raise Failed("sign-in is offered but no tool says it needs it (securitySchemes)")
            response = self.post(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "tools/call",
                    "params": {"name": protected[0], "arguments": {}},
                }
            )
            if response.status_code != 401:
                raise Failed(
                    f"{protected[0]} without a token: HTTP {response.status_code}, not 401"
                )
            header = response.headers.get("www-authenticate")
            if not header:
                raise Failed("a 401 without WWW-Authenticate")
            fields = bearer_challenge(header)
            if not fields.get("resource_metadata"):
                raise Failed("the challenge has no resource_metadata")
            if not fields.get("scope"):
                raise Failed("the challenge names no scope")
            state.update(fields)
            return f"{protected[0]}: 401, scope {fields['scope']}"

        def resource() -> str:
            document = _object(_json(self.get(state["resource_metadata"])), "the metadata")
            if document.get("resource") != self.endpoint:
                raise Failed(f"resource {document.get('resource')!r}, not {self.endpoint}")
            servers = document.get("authorization_servers")
            if not isinstance(servers, list) or not servers:
                raise Failed("no authorization_servers")
            state["issuer"] = str(servers[0]).rstrip("/")
            return f"sign-in server {state['issuer']}"

        def server() -> str:
            url = f"{state['issuer']}/.well-known/oauth-authorization-server"
            document = _object(_json(self.get(url)), "the server metadata")
            if str(document.get("issuer", "")).rstrip("/") != state["issuer"]:
                raise Failed(f"issuer {document.get('issuer')!r}, not {state['issuer']}")
            for name in ("token_endpoint", "jwks_uri"):
                if not _web_address(str(document.get(name, ""))):
                    raise Failed(f"no https {name}")
            if "authorization_endpoint" in document:
                if "S256" not in (document.get("code_challenge_methods_supported") or []):
                    raise Failed("the browser sign-in does not offer PKCE S256")
            state["jwks_uri"] = document["jwks_uri"]
            state["metadata"] = document
            grants = ", ".join(document.get("grant_types_supported") or [])
            return f"grants: {grants}"

        def keys() -> str:
            document = _object(_json(self.get(state["jwks_uri"])), "the key set")
            found = document.get("keys")
            if not isinstance(found, list) or not found:
                raise Failed("no keys")
            if any(key.get("kty") != "RSA" for key in found if isinstance(key, dict)):
                raise Failed("a key that is not RSA (tokens are RS256)")
            return f"{len(found)} RSA key(s)"

        def wrong_token() -> str:
            response, _ = self.call(
                "tools/call", {"name": protected[0], "arguments": {}}, token=NOT_A_TOKEN
            )
            if response.status_code != 401:
                raise Failed(f"HTTP {response.status_code}, not 401")
            fields = bearer_challenge(response.headers.get("www-authenticate", ""))
            if fields.get("error") != "invalid_token":
                raise Failed(f"error {fields.get('error')!r}, not invalid_token")
            return "401 invalid_token"

        def wallet_path() -> str:
            metadata = state["metadata"]
            if WALLET_GRANT not in (metadata.get("grant_types_supported") or []):
                raise Failed("the sign-in server does not offer the wallet-signature grant")
            endpoint = str(metadata.get("dmd_wallet_challenge_endpoint", ""))
            if not _web_address(endpoint):
                raise Failed("no https dmd_wallet_challenge_endpoint")
            unknown = f"did:web:{urlsplit(self.endpoint).hostname}:agents:handshake-check"
            for url, form in (
                (endpoint, {"did": unknown}),
                (metadata["token_endpoint"], {"grant_type": WALLET_GRANT, "message": "x"}),
            ):
                response = self.http.post(url, data=form, headers={"Accept": JSON_TYPE})
                if response.status_code != 400 or _content_type(response) != JSON_TYPE:
                    raise Failed(f"{url}: HTTP {response.status_code}, not a 400 OAuth error")
                if not _object(response.json(), "the error").get("error"):
                    raise Failed(f"{url}: no OAuth error code")
            return "challenge and token requests answer (a wrong one refused, 400)"

        if not self.step("sign-in: 401 challenge without a token", challenge):
            return
        if not self.step("sign-in: protected resource metadata", resource):
            return
        if self.step("sign-in: authorization server metadata", server):
            self.step("sign-in: token keys (JWKS)", keys)
            if "wallet-signature" in self.authentication:
                self.step("sign-in: wallet-signature responds", wallet_path)
        self.step("sign-in: a wrong token is refused", wrong_token)

    # ----------------------------------------------------------------------- the Registry

    def registry_listing(self) -> str:
        assert self.registry is not None
        name = _text_field(self.server_card, "name", "the Server Card")
        response = self.registry.get(
            f"{self.registry_url}/v0.1/servers", params={"search": name}, timeout=TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            raise Warned(f"the Registry answered HTTP {response.status_code}: try again later")
        entries = _object(response.json(), "the Registry's answer").get("servers") or []
        listed = []
        for entry in entries:
            server = entry.get("server", entry) if isinstance(entry, dict) else {}
            if isinstance(server, dict) and server.get("name") == name:
                meta = (entry.get("_meta") or {}).get("io.modelcontextprotocol.registry/official")
                latest = not isinstance(meta, dict) or meta.get("isLatest", True)
                listed.append((latest, server))
        if not listed:
            raise Failed(f"{name} is not listed: publish it again (docs/production.md, step 13)")
        server = next((server for latest, server in listed if latest), listed[0][1])
        urls = [remote.get("url") for remote in server.get("remotes") or []]
        if self.endpoint not in urls:
            raise Failed(f"the listing names another address: {', '.join(map(str, urls))}")
        for key in ("title", "description"):
            if server.get(key) != self.server_card.get(key):
                raise Failed(f"the listing's {key} is not the card's")
        if server.get("version") != self.version:
            raise Warned(
                f"listed as {server.get('version')}, this server is {self.version}: publish "
                "the new version when the listing should show it (docs/production.md, step 13)"
            )
        return f"listed, version {self.version}"


# --------------------------------------------------------------------------- the command


class Loopback(httpx.HTTPTransport):
    """Every request goes to this server's nginx (127.0.0.1), under its public name: the Host
    header and the TLS server name (SNI) stay the public ones."""

    def __init__(self, port: int = 443) -> None:
        super().__init__(verify=False)  # Cloudflare's origin certificate: see the docstring
        self.port = port

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        name = request.url.host
        if request.url.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": name}
        request.url = request.url.copy_with(host="127.0.0.1")
        return super().handle_request(request)


def build_client(*, local: bool, timeout: float = TIMEOUT_SECONDS) -> httpx.Client:
    headers = {"User-Agent": "dmd-gateway-handshake (DebitMyData discovery check)"}
    if local:
        return httpx.Client(transport=Loopback(), headers=headers, timeout=timeout)
    return httpx.Client(headers=headers, timeout=timeout)


def check(
    url: str,
    *,
    local: bool = False,
    registry: bool = False,
    http: httpx.Client | None = None,
    registry_http: httpx.Client | None = None,
) -> Report:
    own = http is None
    client = http or build_client(local=local)
    listing = registry_http or (build_client(local=False) if registry else None)
    try:
        return Handshake(url, client, registry=listing).run()
    finally:
        if own:
            client.close()
        if listing is not None and registry_http is None:
            listing.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dmd-gateway-handshake",
        description="What an agent does to find and use the Agentic Superhighway, checked.",
    )
    parser.add_argument("url", help="the gateway, e.g. https://api.debitmydata.io")
    parser.add_argument("--local", action="store_true", help="ask this server's nginx")
    parser.add_argument("--registry", action="store_true", help="check the MCP Registry too")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)
    if not _web_address(args.url):
        parser.error("the URL must be https://<host> (http only for localhost)")
    report = check(args.url, local=args.local, registry=args.registry)
    if args.json:
        payload = {
            "url": report.url,
            "ok": not report.failed,
            "summary": report.summary(),
            "results": [asdict(result) for result in report.results],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(report.text())
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
