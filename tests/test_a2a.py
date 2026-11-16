"""A2A, the Agent2Agent protocol (api/a2a.py): the Agent Card, the JSON-RPC endpoint in A2A
1.0 and 0.3, and the MCP server's public tools run as A2A skills."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from .conftest import TEST_DATABASE_URL
from .fakes import AGENT_CONTROLLER
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import ISSUER, clean_chain, engine  # noqa: F401

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
CARD = "/.well-known/agent-card.json"
A2A = "/agenticsuperhighway/a2a"
V1 = {"A2A-Version": "1.0"}
PUBLIC_SKILLS = {
    "debitmydata_discover",
    "debitmydata_get_did_registration_offer",
    "debitmydata_open_checkout",
    "debitmydata_create_did_payment_quote",
    "debitmydata_get_payment_status",
    "debitmydata_prove_wallet_control",
    "debitmydata_register_did",
    "debitmydata_verify_agent",
}


def rpc(
    client: TestClient,
    method: str,
    params: Any = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 7, "method": method, **extra}
    if params is not None:
        body["params"] = params
    response = client.post(A2A, json=body, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def ask(
    client: TestClient, parts: list[dict[str, Any]], *, version: str = "1.0", **message: Any
) -> dict[str, Any]:
    """SendMessage (1.0) or message/send (0.3); returns the answer's message."""
    sent = {"messageId": "m-1", "parts": parts, **message}
    if version == "1.0":
        sent["role"] = "ROLE_USER"
        answer = rpc(client, "SendMessage", {"message": sent}, V1)
        return answer["result"]["message"]
    sent.update(kind="message", role="user")
    return rpc(client, "message/send", {"message": sent})["result"]


def data_of(message: dict[str, Any]) -> dict[str, Any]:
    """The answer's data part, which its text part repeats as JSON."""
    texts = [part["text"] for part in message["parts"] if "text" in part]
    datas = [part["data"] for part in message["parts"] if "data" in part]
    assert len(texts) == len(datas) == 1
    assert json.loads(texts[0]) == datas[0]
    return datas[0]


def skill(name: str, **arguments: Any) -> list[dict[str, Any]]:
    return [{"data": {"skill": name, "arguments": arguments}, "mediaType": "application/json"}]


# --------------------------------------------------------------------------- the Agent Card


def test_the_agent_card_says_where_and_what(client: TestClient) -> None:
    response = client.get(CARD)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == "public, max-age=300"
    etag = response.headers["etag"]
    assert client.get(CARD, headers={"If-None-Match": etag}).status_code == 304
    assert client.head(CARD).status_code == 200
    card = response.json()
    url = f"http://localhost:8000{A2A}"
    assert card["name"] == "DebitMyData Agentic Superhighway (development)"
    assert card["supportedInterfaces"] == [
        {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "0.3"},
    ]
    # 0.3 clients read these (1.0 clients drop them when supportedInterfaces is there).
    assert (card["url"], card["preferredTransport"], card["protocolVersion"]) == (
        url,
        "JSONRPC",
        "0.3.0",
    )
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False}
    assert "securitySchemes" not in card  # no sign-in needed, as for the MCP tools
    assert card["provider"] == {"organization": "DebitMyData", "url": "https://debitmydata.com/"}
    assert card["documentationUrl"] == "http://localhost:8000/llms.txt"
    assert "$25.00 USD" in card["description"] and '{"skill": "<id>"' in card["description"]
    for name in ("name", "description", "version", "defaultInputModes", "defaultOutputModes"):
        assert card[name], name


def test_every_skill_is_a_public_mcp_tool_with_how_to_call_it(client: TestClient) -> None:
    skills = {item["id"]: item for item in client.get(CARD).json()["skills"]}
    assert PUBLIC_SKILLS <= set(skills)
    assert "debitmydata_whoami" not in skills  # sign-in tools stay on the MCP server
    for name, item in skills.items():
        assert item["name"] and item["description"] and "did-llm" in item["tags"], name
        (example,) = item["examples"]
        call = json.loads(example)
        assert call["skill"] == name and isinstance(call["arguments"], dict)
    quote = json.loads(skills["debitmydata_create_did_payment_quote"]["examples"][0])
    assert "display_name" in quote["arguments"]


# --------------------------------------------------------------------------- the protocol


def test_protocol_errors_are_json_rpc_errors(client: TestClient) -> None:
    def error_of(answer: dict[str, Any]) -> int:
        assert "result" not in answer
        return answer["error"]["code"]

    wrong_type = client.post(
        A2A, content=b'{"jsonrpc":"2.0"}', headers={"Content-Type": "text/plain"}
    )
    assert error_of(wrong_type.json()) == -32600
    not_json = client.post(A2A, content=b"{oops", headers={"Content-Type": "application/json"})
    assert error_of(not_json.json()) == -32700 and not_json.json()["id"] is None
    batch = client.post(A2A, json=[{"jsonrpc": "2.0", "id": 1, "method": "GetTask"}])
    assert error_of(batch.json()) == -32600
    assert error_of(client.post(A2A, json={"id": 1, "method": "GetTask"}).json()) == -32600
    assert error_of(rpc(client, "tasks/send", {})) == -32601
    assert error_of(rpc(client, "SendMessage", {}, V1)) == -32602
    assert error_of(rpc(client, "SendMessage", {"message": {"parts": []}}, V1)) == -32602
    # No tasks are kept, nothing streams, nothing is pushed, no extended card.
    assert error_of(rpc(client, "GetTask", {"id": "t-1"}, V1)) == -32001
    assert error_of(rpc(client, "tasks/cancel", {"id": "t-1"})) == -32001
    assert error_of(rpc(client, "SendStreamingMessage", {"message": {}}, V1)) == -32004
    assert error_of(rpc(client, "CreateTaskPushNotificationConfig", {}, V1)) == -32003
    assert error_of(rpc(client, "GetExtendedAgentCard", {}, V1)) == -32007
    assert rpc(client, "ListTasks", {}, V1)["result"] == {"tasks": []}
    # Versions: 1.0, or none for 0.3; anything else is refused with the supported ones.
    refused = rpc(client, "SendMessage", {}, {"A2A-Version": "2.0"})
    assert refused["error"]["code"] == -32009
    assert refused["error"]["data"] == {"supportedVersions": ["1.0", "0.3"]}
    query = client.post(
        f"{A2A}?A2A-Version=1.0", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask"}
    )
    assert query.json()["error"]["code"] == -32001
    assert rpc(client, "GetTask", {}, {"A2A-Version": "1.0.0"})["error"]["code"] == -32001
    assert rpc(client, "GetTask", {}, extra := {})["id"] == 7 and not extra


@pytest.mark.parametrize("version", ["1.0", "0.3"])
def test_a_message_without_a_skill_gets_the_list_of_skills(
    client: TestClient, version: str
) -> None:
    text = {"text": "Hello, what can you do?"}
    message = ask(client, [text if version == "1.0" else {"kind": "text", **text}], version=version)
    if version == "1.0":
        assert message["role"] == "ROLE_AGENT" and "kind" not in message
        assert all(part.get("mediaType") == "application/json" for part in message["parts"])
    else:
        assert message["role"] == "agent" and message["kind"] == "message"
        assert [part["kind"] for part in message["parts"]] == ["text", "data"]
    assert message["messageId"] and message["contextId"]
    assert message["metadata"] == {"skill": None, "error": False}
    help_ = data_of(message)
    assert '{"skill": "<skill>", "arguments": {...}}' in help_["how_to_call"]
    assert help_["agent_card"] == f"http://localhost:8000{CARD}"
    listed = {item["skill"]: item for item in help_["skills"]}
    assert PUBLIC_SKILLS <= set(listed)
    quote = listed["debitmydata_create_did_payment_quote"]["arguments"]
    assert "display_name" in quote["properties"]  # each skill's arguments, as a JSON Schema
    # The client's context goes on.
    again = ask(client, [{"text": "hi"}], contextId="ctx-42")
    assert again["contextId"] == "ctx-42"


def test_an_unknown_skill_or_bad_arguments_are_answered_not_raised(client: TestClient) -> None:
    unknown = data_of(ask(client, skill("debitmydata_whoami")))
    assert unknown["error"]["code"] == "unknown_skill"
    assert "skills" in unknown  # and the list of those there are
    bad = ask(client, [{"data": {"skill": "debitmydata_get_payment_status", "arguments": []}}])
    assert data_of(bad)["error"]["code"] == "invalid_arguments"
    assert bad["metadata"] == {"skill": "debitmydata_get_payment_status", "error": True}
    missing = data_of(ask(client, skill("debitmydata_get_payment_status")))
    assert "payment_intent_id" in json.dumps(missing["error"])


# --------------------------------------------------------------------------- skills, for real


@pytest.fixture
def gateway(ready_registry: Path, engine: Engine) -> Iterator[TestClient]:  # noqa: F811
    with _app_client(ready_registry, engine, issuer_key=ISSUER) as client:
        yield client


@needs_db
@pytest.mark.parametrize("version", ["1.0", "0.3"])
def test_an_agent_gets_a_quote_and_follows_its_payment_over_a2a(
    gateway: TestClient, version: str
) -> None:
    def run(name: str, **arguments: Any) -> dict[str, Any]:
        parts = skill(name, **arguments)
        if version == "0.3":
            parts = [{"kind": "data", "data": parts[0]["data"]}]
        message = ask(gateway, parts, version=version)
        assert message["metadata"]["skill"] == name
        return data_of(message)

    offer = run("debitmydata_get_did_registration_offer")
    assert offer["offer"]["price_text"] == "$25.00 USD, paid once"
    quote = run(
        "debitmydata_create_did_payment_quote",
        display_name="A2A test agent",
        participant_type="autonomous_agent",
        controller_public_key=AGENT_CONTROLLER,
        preferred_payment_option="dmd_pow_native",
    )
    assert quote["payment_intent_id"].startswith("dmd_payment_")
    status = run("debitmydata_get_payment_status", payment_intent_id=quote["payment_intent_id"])
    assert status["status"] == "awaiting_payment"
    # A refusal from the gateway comes back as data: its code and message, as over REST.
    unknown = "dmd_payment_intent_" + "0" * 26
    refused = run("debitmydata_get_payment_status", payment_intent_id=unknown)
    assert refused["http_status"] == 404 and refused["error"]["code"] == "not_found"
    # Arguments the skill refuses: what is wrong with them, without the tool's prefix.
    wrong = run("debitmydata_get_payment_status", payment_intent_id="dmd_payment_x")
    assert wrong["error"]["code"] == "invalid_arguments"
    assert "payment_intent_id" in wrong["error"]["message"]
    assert not wrong["error"]["message"].startswith("Error executing tool")


@needs_db
def test_a_skill_named_in_a_text_part_runs_too(gateway: TestClient) -> None:
    """Clients that only send text can send the call as JSON text."""
    call = json.dumps({"skill": "debitmydata_discover"})
    found = data_of(ask(gateway, [{"text": call}]))
    assert found["product"]["name"] == "DebitMyData DID-LLM"


def test_agents_reading_llms_txt_learn_about_a2a(client: TestClient) -> None:
    text = client.get("/llms.txt").text
    assert "http://localhost:8000/.well-known/agent-card.json" in text
    assert "http://localhost:8000/agenticsuperhighway/a2a" in text
    assert '`{"skill": "<tool name>", "arguments": {...}}`' in text
