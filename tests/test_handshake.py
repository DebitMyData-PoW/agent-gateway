"""The discovery runbook's Day 5 check (dmd-gateway-handshake): what an agent does to find and
use the Agentic Superhighway, run against the gateway itself; that it catches what goes wrong
(wrong content type, tools that differ, a missing Registry listing); the Registry check; the
loopback transport the update scripts use; and the command."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from dmd_gateway import __version__, handshake
from dmd_gateway.handshake import FAIL, PASS, SKIP, WARN, Handshake, Loopback, Report, Result
from dmd_gateway.oauth import keys

from .conftest import TEST_DATABASE_URL
from .test_mcp_auth import ISSUER
from .test_oauth import AUTH
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import clean_chain, engine  # noqa: F401

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
BASE = "http://localhost:8000"
MCP = f"{BASE}/agenticsuperhighway"


def through(client: TestClient, change: Callable[[httpx.Request, Any], Any] | None = None) -> Any:
    """An httpx client whose requests the gateway answers, each answer changed by ``change``."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = client.request(
            request.method,
            str(request.url),
            headers={k: v for k, v in request.headers.items() if k != "content-length"},
            content=request.content,
        )
        headers = {k: v for k, v in answer.headers.items() if k != "content-length"}
        response = httpx.Response(answer.status_code, headers=headers, content=answer.content)
        return change(request, response) if change else response

    return httpx.Client(transport=httpx.MockTransport(handler))


def statuses(report: Report) -> dict[str, str]:
    return {result.step: result.status for result in report.results}


def test_the_gateway_passes_the_handshake(client: TestClient) -> None:
    report = Handshake(BASE, client).run()
    found = statuses(report)
    assert not report.failed, report.text()
    assert found["sign-in (OAuth 2.1)"] == SKIP  # closed here: no auth host
    assert all(status == PASS for step, status in found.items() if step != "sign-in (OAuth 2.1)")
    assert len(found) == 15
    details = {result.step: result.detail for result in report.results}
    assert details["discovery: /.well-known/mcp"].startswith(f"MCP at {MCP};")
    assert details["one truth: the same version everywhere"] == f"version {__version__}"
    assert "naming" in details["json-rpc: a wrong argument is named"]
    assert report.summary() == "handshake complete: 14 checks passed"
    assert report.text().splitlines()[0] == f"Handshake with {BASE}"


@pytest.fixture
def signing_in(ready_registry: Path, engine: Engine, tmp_path: Path) -> Iterator[TestClient]:  # noqa: F811
    key_path = tmp_path / "oauth.pem"
    keys.write_new_key(key_path)
    overrides = {"auth_host": AUTH, "oauth_key_path": key_path}
    with _app_client(
        ready_registry, engine, issuer_key=ISSUER, settings_overrides=overrides
    ) as client:
        yield client


@needs_db
def test_the_sign_in_negotiation_completes(signing_in: TestClient) -> None:
    report = Handshake(BASE, signing_in).run()
    assert not report.failed and not report.warnings, report.text()
    details = {result.step: result.detail for result in report.results}
    assert details["sign-in: 401 challenge without a token"] == "dmd_whoami: 401, scope did:read"
    assert details["sign-in: protected resource metadata"] == f"sign-in server https://{AUTH}"
    assert "authorization_code" in details["sign-in: authorization server metadata"]
    assert details["sign-in: token keys (JWKS)"] == "1 RSA key(s)"
    assert details["sign-in: a wrong token is refused"] == "401 invalid_token"
    assert details["sign-in: wallet-signature responds"].startswith("challenge and token")
    assert "oauth2.1" in details["discovery: /.well-known/mcp"]


def test_what_goes_wrong_is_caught(client: TestClient) -> None:
    def html(request: httpx.Request, response: httpx.Response) -> httpx.Response:
        if request.url.path == "/.well-known/mcp":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")
        return response

    report = Handshake(BASE, through(client, html)).run()
    assert [(r.status, r.detail) for r in report.results] == [
        (FAIL, f"{BASE}/.well-known/mcp is text/html, not application/json")
    ]

    def one_tool_less(request: httpx.Request, response: httpx.Response) -> httpx.Response:
        if request.url.path == "/.well-known/mcp/server-card.json":
            card = response.json()
            card["tools"] = card["tools"][1:]
            return httpx.Response(200, json=card)
        return response

    report = Handshake(BASE, through(client, one_tool_less)).run()
    tools = next(r for r in report.results if r.step == "one truth: the same tools everywhere")
    assert tools.status == FAIL and tools.detail.startswith("only in the card: none; only in")
    assert report.summary() == f"1 of {len(report.results)} checks failed"

    def undescribed(request: httpx.Request, response: httpx.Response) -> httpx.Response:
        if request.method == "POST" and b"tools/list" in request.content:
            answer = response.json()
            tool = answer["result"]["tools"][3]
            next(iter(tool["inputSchema"]["properties"].values())).pop("description")
            return httpx.Response(200, json=answer)
        return response

    report = Handshake(BASE, through(client, undescribed)).run()
    listed = next(r for r in report.results if r.step == "mcp: tools/list")
    assert listed.status == FAIL and "parameters without a description" in listed.detail

    def unreachable(request: httpx.Request, response: httpx.Response) -> httpx.Response:
        if request.url.path == "/llms.txt":
            raise httpx.ConnectError("refused")
        return response

    report = Handshake(BASE, through(client, unreachable)).run()
    assert statuses(report)["discovery: llms.txt"] == FAIL
    assert "no answer: ConnectError" in report.text()


def registry_answer(**changes: Any) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0.1/servers"
        name = request.url.params["search"]
        server = {
            "name": name,
            "title": "DebitMyData Agentic Superhighway (development)",
            "description": "Test server of the Agentic Superhighway: test networks and test "
            "DID-LLMs only.",
            "version": __version__,
            "remotes": [{"type": "streamable-http", "url": MCP}],
            **changes,
        }
        meta = {"io.modelcontextprotocol.registry/official": {"isLatest": True}}
        return httpx.Response(200, json={"servers": [{"server": server, "_meta": meta}]})

    return handler


@pytest.mark.parametrize(
    ("handler", "status", "words"),
    [
        (registry_answer(), PASS, f"listed, version {__version__}"),
        (registry_answer(version="0.1.0"), WARN, "listed as 0.1.0"),
        (registry_answer(name="io.other/server"), FAIL, "is not listed: publish it again"),
        (registry_answer(remotes=[{"url": "https://evil.example/mcp"}]), FAIL, "another address"),
        (registry_answer(description="Something else"), FAIL, "description is not the card's"),
        (lambda request: httpx.Response(503), WARN, "HTTP 503"),
    ],
)
def test_the_registry_listing(
    client: TestClient, handler: Callable[[httpx.Request], httpx.Response], status: str, words: str
) -> None:
    registry = httpx.Client(transport=httpx.MockTransport(handler))
    report = Handshake(BASE, client, registry=registry).run()
    listing = report.results[-1]
    assert listing.step == "MCP Registry: listed as the card says"
    assert listing.status == status and words in listing.detail


def test_the_loopback_asks_this_server_under_its_public_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def answer(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", answer)
    with httpx.Client(transport=Loopback()) as http:
        http.get("https://api.debitmydata.io/.well-known/mcp")
    [request] = seen
    assert str(request.url) == "https://127.0.0.1/.well-known/mcp"
    assert request.headers["host"] == "api.debitmydata.io"
    assert request.extensions["sni_hostname"] == "api.debitmydata.io"


def test_the_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[dict[str, Any]] = []

    def fake(url: str, **options: Any) -> Report:
        calls.append({"url": url, **options})
        results = [Result("discovery: /.well-known/mcp", PASS, "MCP at ...")]
        if "staging" in url:
            results.append(Result("mcp: initialize", FAIL, "HTTP 502 at ..."))
        return Report(url, results)

    monkeypatch.setattr(handshake, "check", fake)
    assert handshake.main(["https://api.debitmydata.io", "--local", "--registry"]) == 0
    assert calls[-1] == {"url": "https://api.debitmydata.io", "local": True, "registry": True}
    out = capsys.readouterr().out
    assert "PASS  discovery: /.well-known/mcp  MCP at ..." in out
    assert out.strip().endswith("handshake complete: 1 checks passed")

    assert handshake.main(["https://staging-api.debitmydata.io", "--json"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False and printed["results"][1]["status"] == FAIL
    with pytest.raises(SystemExit):
        handshake.main(["http://api.debitmydata.io"])


def test_the_command_against_the_gateway(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(handshake, "build_client", lambda **_: through(client))
    assert handshake.main([BASE]) == 0
    assert "handshake complete" in capsys.readouterr().out


def test_the_daily_report_says_what_agents_see() -> None:
    from dmd_gateway.monitoring.alerts import Severity
    from dmd_gateway.monitoring.reconcile import _discovery

    lines: list[str] = []
    good = Report(BASE, [Result("mcp: initialize", PASS, "protocol ...")])
    assert _discovery(lambda: good, "2026-11-14", lines.append) == []
    assert lines == [f"- {BASE}: handshake complete: 1 checks passed"]

    lines.clear()
    bad = Report(
        BASE,
        [
            Result("discovery: /.well-known/mcp", PASS, "MCP at ..."),
            Result("mcp: initialize", FAIL, "HTTP 502"),
            Result("MCP Registry: listed as the card says", WARN, "listed as 0.37.0"),
        ],
    )
    [finding] = _discovery(lambda: bad, "2026-11-14", lines.append)
    assert finding.key == "discovery.handshake.2026-11-14" and finding.kind == "event"
    assert finding.severity == Severity.WARNING
    assert finding.detail.startswith("mcp: initialize: HTTP 502. To see them all:")
    assert lines[1:] == [
        "- FAIL: mcp: initialize: HTTP 502",
        "- WARN: MCP Registry: listed as the card says: listed as 0.37.0",
    ]

    def broken() -> Report:
        raise RuntimeError("boom")

    lines.clear()
    assert _discovery(broken, "2026-11-14", lines.append) == []
    assert lines == ["- could not run: RuntimeError"]
