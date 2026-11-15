"""Owner changes and owner revocation (ID-12; services/owner_changes.py, api/owners.py): a
current owner approves and each new owner accepts, by signing; every sign-in from before the
change ends; an owner revokes the DID-LLM for good. Through the REST API, the MCP server and
the owner-key tool."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from dmd_gateway.db.session import session_factory, transaction
from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import encode_signature, parse_controller_key
from dmd_gateway.oauth.grants import before_owner_change
from dmd_gateway.services import owner_changes
from dmd_gateway.services.owner_changes import Owners
from dmd_gateway.signer.client import SignerRefused

from .conftest import REPO_ROOT, TEST_DATABASE_URL
from .fakes import AGENT_CONTROLLER, AGENT_KEY, sign_challenge
from .test_identity_status import issue, run
from .test_mcp_auth import rpc, whoami
from .test_oauth_browser import gateway, token  # noqa: F401
from .test_quotes import ready_registry  # noqa: F401
from .test_registration import clean_chain, engine  # noqa: F401
from .test_sign_in_grants import ask_token, exchange, production, stored_grants  # noqa: F401
from .test_wallet_registration import wallet_signature

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
HOST = {"host": "localhost:8000"}
BASE = "http://localhost:8000"


def new_key() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, parse_controller_key("ed25519:" + raw.hex()).canonical


def sign(key: Ed25519PrivateKey, message: str) -> str:
    return encode_signature(key.sign(message.encode("utf-8")))


def start(client: TestClient, did: str, **owners: str) -> dict[str, Any]:
    response = client.post(f"/v1/dids/{did}/owner-changes", json=owners, headers=HOST)
    assert response.status_code == 201, response.text
    return response.json()


def send(
    client: TestClient, did: str, change: dict[str, Any], item: dict[str, Any], sig: str
) -> Any:
    return client.post(
        f"/v1/dids/{did}/owner-changes/{change['change_id']}/signatures",
        json={"challenge_id": item["challenge_id"], "signature": sig},
        headers=HOST,
    )


def item(change: dict[str, Any], role: str, signer: str) -> dict[str, Any]:
    (found,) = [m for m in change["messages"] if m["role"] == role and m["signer"] == signer]
    return found


def record(client: TestClient, did: str) -> dict[str, Any]:
    response = client.get(f"/v1/dids/{did}", headers=HOST)
    assert response.status_code == 200, response.text
    return response.json()


def verify(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/v1/dids/verifications", json=body, headers=HOST)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- no database


def test_a_sign_in_from_before_an_owner_change_no_longer_counts() -> None:
    changed = datetime(2026, 11, 15, 12, 0, 0, 400_000, tzinfo=UTC)
    second = int(changed.timestamp())
    assert before_owner_change(second - 1, changed) is True
    assert before_owner_change(second, changed) is True  # the change's own second: not either
    assert before_owner_change(second + 1, changed) is False
    assert before_owner_change(changed - timedelta(days=3), changed) is True
    assert before_owner_change(second - 1, None) is False  # never changed


def test_the_messages_spell_everything_out() -> None:
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    did = "did:web:api.debitmydata.io:agents:01KAAAAAAAAAAAAAAAAAAAAAAA"
    text_ = owner_changes.change_message(
        audience="https://api.debitmydata.io",
        did=did,
        change_id="dmd_owner_change_01KAAAAAAAAAAAAAAAAAAAAAAA",
        signer=AGENT_CONTROLLER,
        signer_role="approve",
        owners_now={"ed25519": AGENT_CONTROLLER},
        new_owners={"eip155": "eip155:8453:0x" + "Ab" * 20},
        nonce="n0nce",
        issued_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    assert text_.split("\n") == [
        "DebitMyData owner change",
        "Audience: https://api.debitmydata.io",
        "Purpose: rotation",
        f"DID-LLM: {did}",
        "Change: dmd_owner_change_01KAAAAAAAAAAAAAAAAAAAAAAA",
        f"Signer: {AGENT_CONTROLLER}",
        "Role: approve (a current owner hands the DID-LLM over)",
        f"Owners now: key {AGENT_CONTROLLER}",
        "New owners: wallet eip155:8453:0x" + "Ab" * 20,
        "Effect: only the new owners control the DID-LLM; sign-ins made before the change end",
        "Nonce: n0nce",
        "Issued: 2026-11-15T12:00:00Z",
        "Expires: 2026-11-15T12:15:00Z",
    ]
    assert owner_changes.clean_reason(None) == "revoked by its owner"
    assert owner_changes.clean_reason("  Key\n leaked  ") == "Key leaked"
    for bad in ("ab", "Key leaked‮ desrever", "x" * 301):
        with pytest.raises(ApiError):
            owner_changes.clean_reason(bad)


def test_a_reserved_name_stays_with_debitmydata() -> None:
    _, brand_key = new_key()
    _, stranger = new_key()
    reserved = SimpleNamespace(display_name="DebitMyData Support Agent")
    ordinary = SimpleNamespace(display_name="Research Agent")
    brand = frozenset({brand_key})
    with pytest.raises(ApiError) as refused:
        owner_changes._check_reserved_name(reserved, Owners(stranger, None), brand)  # type: ignore[arg-type]  # noqa: SLF001
    assert refused.value.code == "reserved_name"
    owner_changes._check_reserved_name(reserved, Owners(brand_key, None), brand)  # type: ignore[arg-type]  # noqa: SLF001
    owner_changes._check_reserved_name(ordinary, Owners(stranger, None), brand)  # type: ignore[arg-type]  # noqa: SLF001


# --------------------------------------------------------------------------- owner changes


@needs_db
def test_a_key_owner_moves_to_a_new_key(gateway: TestClient, engine: Engine) -> None:  # noqa: F811
    issued = issue(gateway)
    did = issued["did"]
    assert record(gateway, did)["owner_changed_at"] is None
    key2, owner2 = new_key()
    change = start(gateway, did, new_controller_public_key=owner2)
    assert change["status"] == "open" and change["did"] == did
    assert change["previous_owners"] == [{"type": "key", "value": AGENT_CONTROLLER}]
    assert change["new_owners"] == [{"type": "key", "value": owner2}]
    assert change["waiting_for"] == {"approval": True, "acceptances": [owner2]}
    approve, accept = item(change, "approve", AGENT_CONTROLLER), item(change, "accept", owner2)
    assert approve["status"] == accept["status"] == "open"
    lines = approve["message"].split("\n")
    assert lines[:4] == [
        "DebitMyData owner change",
        f"Audience: {BASE}",
        "Purpose: rotation",
        f"DID-LLM: {did}",
    ]
    assert f"Change: {change['change_id']}" in lines and f"New owners: key {owner2}" in lines
    assert change["signatures_url"] == (
        f"{BASE}/v1/dids/{did}/owner-changes/{change['change_id']}/signatures"
    )

    first = send(gateway, did, change, approve, sign_challenge(approve["message"]))
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "open"
    assert first.json()["waiting_for"] == {"approval": False, "acceptances": [owner2]}
    assert record(gateway, did)["controller_public_key"] == AGENT_CONTROLLER  # not yet

    done = send(gateway, did, change, accept, sign(key2, accept["message"]))
    assert done.status_code == 201, done.text
    applied = done.json()
    assert applied["status"] == "applied" and applied["applied_at"]
    assert applied["waiting_for"] == {"approval": False, "acceptances": []}
    assert {m["status"] for m in applied["messages"]} == {"verified"}
    again = gateway.get(f"/v1/dids/{did}/owner-changes/{change['change_id']}", headers=HOST)
    assert again.json()["status"] == "applied"

    now = record(gateway, did)
    assert now["controller_public_key"] == owner2 and now["owner_changed_at"]
    document = gateway.get(
        f"/agents/{issued['identity_id'].removeprefix('dmd_identity_')}/did.json", headers=HOST
    ).json()
    assert [m["publicKeyMultibase"] for m in document["verificationMethod"]] == [
        owner2.removeprefix("ed25519:")
    ]
    # The new key proves control; the old one no longer does. The credential stays.
    asked = verify(gateway, did=did)["challenge"]["message"]
    assert verify(gateway, did=did, message=asked, signature=sign(key2, asked))["verified"]
    old = verify(gateway, did=did, message=asked, signature=sign_challenge(asked))
    assert old["verified"] is False
    assert (
        gateway.get(f"/v1/credentials/{issued['credential_id']}", headers=HOST).status_code == 200
    )
    with engine.connect() as connection:
        (data,) = connection.execute(
            text(
                "SELECT data FROM audit_events WHERE action = 'identity.owner_changed' "
                "AND subject_id = :s"
            ),
            {"s": issued["identity_id"]},
        ).one()
    assert data["from"] == {"ed25519": AGENT_CONTROLLER} and data["to"] == {"ed25519": owner2}

    # Next time, the new key approves.
    _, owner3 = new_key()
    following = start(gateway, did, new_controller_public_key=owner3)
    assert [m["signer"] for m in following["messages"] if m["role"] == "approve"] == [owner2]


@needs_db
def test_what_an_owner_change_refuses(gateway: TestClient, engine: Engine) -> None:  # noqa: F811
    did = issue(gateway)["did"]
    post = lambda body: gateway.post(f"/v1/dids/{did}/owner-changes", json=body, headers=HOST)  # noqa: E731
    same = post({"new_controller_public_key": AGENT_CONTROLLER})
    assert same.status_code == 409 and same.json()["error"]["code"] == "no_change"
    assert post({}).status_code == 422
    assert post({"new_controller_public_key": "ed25519:nope"}).status_code == 422
    assert post({"new_controller_wallet_address": "0x" + "aB" * 20}).status_code == 422  # checksum
    elsewhere = post(
        {
            "new_controller_wallet_address": Account.create().address,
            "new_controller_network": "eip155:1",
        }
    )
    assert elsewhere.status_code == 422
    assert elsewhere.json()["error"]["code"] == "invalid_controller_wallet"
    unknown = gateway.post(
        "/v1/dids/did:web:localhost%3A8000:agents:01KAAAAAAAAAAAAAAAAAAAAAAA/owner-changes",
        json={"new_controller_public_key": new_key()[1]},
        headers=HOST,
    )
    assert unknown.status_code == 404

    key2, owner2 = new_key()
    change = start(gateway, did, new_controller_public_key=owner2)
    approve, accept = item(change, "approve", AGENT_CONTROLLER), item(change, "accept", owner2)
    # A new owner's acceptance signed by someone else, and a wrong approval: both used up.
    wrong = send(gateway, did, change, accept, sign_challenge(accept["message"]))
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "invalid_signature"
    assert send(gateway, did, change, accept, sign(key2, accept["message"])).status_code == 409
    stranger = sign(Ed25519PrivateKey.generate(), approve["message"])
    assert send(gateway, did, change, approve, stranger).status_code == 422
    view = gateway.get(f"/v1/dids/{did}/owner-changes/{change['change_id']}", headers=HOST).json()
    assert view["waiting_for"] == {"approval": True, "acceptances": [owner2]}
    assert {m["status"] for m in view["messages"]} == {"rejected"}
    assert record(gateway, did)["controller_public_key"] == AGENT_CONTROLLER

    # Another change applied first cancels the ones still open.
    pending = start(gateway, did, new_controller_public_key=new_key()[1])
    key3, owner3 = new_key()
    winner = start(gateway, did, new_controller_public_key=owner3)
    w_approve = item(winner, "approve", AGENT_CONTROLLER)
    send(gateway, did, winner, w_approve, sign_challenge(w_approve["message"]))
    w_accept = item(winner, "accept", owner3)
    assert send(gateway, did, winner, w_accept, sign(key3, w_accept["message"])).status_code == 201
    late = item(pending, "approve", AGENT_CONTROLLER)
    closed = send(gateway, did, pending, late, sign_challenge(late["message"]))
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "owner_change_closed"

    # Expired: nothing applies after 15 minutes.
    key4, owner4 = new_key()
    slow = start(gateway, did, new_controller_public_key=owner4)
    s_approve = item(slow, "approve", owner3)
    with transaction(session_factory(engine)) as session, pytest.raises(ApiError) as gone:
        owner_changes.submit_signature(
            session,
            did,
            slow["change_id"],
            challenge_id=s_approve["challenge_id"],
            signature=sign(key3, s_approve["message"]),
            now=datetime.now(UTC) + timedelta(minutes=16),
        )
    assert gone.value.status_code == 410

    # Suspended: no owner change.
    code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code == 0
    assert post({"new_controller_public_key": new_key()[1]}).status_code == 409


@needs_db
def test_a_wallet_takes_over_then_both_then_the_key_alone(gateway: TestClient) -> None:  # noqa: F811
    issued = issue(gateway)
    did = issued["did"]
    wallet = Account.create()
    change = start(gateway, did, new_controller_wallet_address=wallet.address)
    account = f"eip155:8453:{wallet.address}"
    assert change["new_owners"] == [{"type": "wallet", "value": account}]
    approve, accept = item(change, "approve", AGENT_CONTROLLER), item(change, "accept", account)
    assert accept["signature_algorithm"].startswith("eip191")
    send(gateway, did, change, approve, sign_challenge(approve["message"]))
    done = send(gateway, did, change, accept, wallet_signature(wallet, accept["message"]))
    assert done.json()["status"] == "applied", done.text
    now = record(gateway, did)
    assert now["controller_public_key"] is None and now["controller_wallet"] == account
    suffix = issued["identity_id"].removeprefix("dmd_identity_")
    methods = gateway.get(f"/agents/{suffix}/did.json", headers=HOST).json()["verificationMethod"]
    assert [m["blockchainAccountId"] for m in methods] == [account]

    # Add a key next to the wallet: the wallet approves, the key accepts.
    key2, owner2 = new_key()
    both = start(
        gateway, did, new_controller_public_key=owner2, new_controller_wallet_address=wallet.address
    )
    assert sorted((m["role"], m["signer"]) for m in both["messages"]) == [
        ("accept", owner2),
        ("approve", account),
    ]
    b_approve = item(both, "approve", account)
    send(gateway, did, both, b_approve, wallet_signature(wallet, b_approve["message"]))
    b_accept = item(both, "accept", owner2)
    assert send(gateway, did, both, b_accept, sign(key2, b_accept["message"])).json()["status"] == (
        "applied"
    )
    now = record(gateway, did)
    assert (now["controller_public_key"], now["controller_wallet"]) == (owner2, account)

    # Drop the wallet: either owner approves, and nobody new has to accept.
    alone = start(gateway, did, new_controller_public_key=owner2)
    assert alone["waiting_for"] == {"approval": True, "acceptances": []}
    k_approve = item(alone, "approve", owner2)
    result = send(gateway, did, alone, k_approve, sign(key2, k_approve["message"])).json()
    assert result["status"] == "applied"
    assert item(result, "approve", account)["status"] == "open"  # not needed
    assert record(gateway, did)["controller_wallet"] is None


@needs_db
def test_room_is_made_never_refused(gateway: TestClient) -> None:  # noqa: F811
    did = issue(gateway)["did"]
    changes = [start(gateway, did, new_controller_public_key=new_key()[1]) for _ in range(4)]
    states = [
        gateway.get(f"/v1/dids/{did}/owner-changes/{c['change_id']}", headers=HOST).json()["status"]
        for c in changes
    ]
    assert states == ["cancelled", "open", "open", "open"]
    asked = [
        gateway.post(f"/v1/dids/{did}/revocations", json={}, headers=HOST).json() for _ in range(4)
    ]
    states = [
        gateway.get(f"/v1/dids/{did}/revocations/{a['challenge_id']}", headers=HOST).json()[
            "status"
        ]
        for a in asked
    ]
    assert states == ["expired", "open", "open", "open"]


# --------------------------------------------------------------------------- sign-ins end


@needs_db
def test_sign_ins_from_before_an_owner_change_end(
    production: tuple[TestClient, Path],  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    client, socket = production
    did = issue(client)["did"]
    tokens = exchange(client, did)  # an app signed in, through the signer
    [(grant, _)] = stored_grants(engine)
    assert whoami(client, tokens["access_token"]).status_code == 200

    key2, owner2 = new_key()
    change = start(client, did, new_controller_public_key=owner2)
    approve, accept = item(change, "approve", AGENT_CONTROLLER), item(change, "accept", owner2)
    send(client, did, change, approve, sign_challenge(approve["message"]))
    assert send(client, did, change, accept, sign(key2, accept["message"])).json()["status"] == (
        "applied"
    )

    refused = whoami(client, tokens["access_token"])
    assert refused.status_code == 401
    assert "owners changed" in refused.json()["error_description"]
    again = token(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert again.json()["error"] == "invalid_grant"
    with pytest.raises(SignerRefused, match="owners changed after this sign-in"):
        ask_token(socket, grant)
    with engine.connect() as connection:
        open_tokens = connection.execute(
            text("SELECT count(*) FROM oauth_refresh_tokens WHERE revoked_at IS NULL")
        ).scalar()
    assert open_tokens == 0


# --------------------------------------------------------------------------- revocation


@needs_db
def test_an_owner_revokes_for_good(gateway: TestClient, engine: Engine) -> None:  # noqa: F811
    issued = issue(gateway)
    did = issued["did"]
    refused = gateway.post(
        f"/v1/dids/{did}/revocations", json={"controller_type": "eip155"}, headers=HOST
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "no_such_controller"
    hidden = gateway.post(f"/v1/dids/{did}/revocations", json={"reason": "Key‮leaked"}, headers=HOST)
    assert hidden.status_code == 422 and hidden.json()["error"]["code"] == "invalid_reason"

    asked = gateway.post(f"/v1/dids/{did}/revocations", json={"reason": "Key leaked"}, headers=HOST)
    assert asked.status_code == 201, asked.text
    challenge = asked.json()
    lines = challenge["message"].split("\n")
    assert lines[:5] == [
        "DebitMyData revocation",
        f"Audience: {BASE}",
        "Purpose: revocation",
        f"DID-LLM: {did}",
        f"Controller: {AGENT_CONTROLLER}",
    ]
    assert "Reason: Key leaked" in lines and any("cannot be undone" in line for line in lines)
    url = f"/v1/dids/{did}/revocations/{challenge['challenge_id']}"
    assert gateway.get(url, headers=HOST).json()["status"] == "open"
    assert challenge["signature_url"] == f"{BASE}{url}/signature"

    stranger = sign(Ed25519PrivateKey.generate(), challenge["message"])
    wrong = gateway.post(f"{url}/signature", json={"signature": stranger}, headers=HOST)
    assert wrong.status_code == 422
    used = gateway.post(
        f"{url}/signature", json={"signature": sign_challenge(challenge["message"])}, headers=HOST
    )
    assert used.status_code == 409
    assert record(gateway, did)["status"] == "active"

    second = gateway.post(
        f"/v1/dids/{did}/revocations", json={"reason": "Key leaked"}, headers=HOST
    )
    message = second.json()
    done = gateway.post(
        f"/v1/dids/{did}/revocations/{message['challenge_id']}/signature",
        json={"signature": sign(AGENT_KEY, message["message"])},
        headers=HOST,
    )
    assert done.status_code == 201, done.text
    assert done.json()["status"] == "revoked"
    now = record(gateway, did)
    assert now["status"] == "revoked" and now["entitlements"] == []
    suffix = issued["identity_id"].removeprefix("dmd_identity_")
    assert gateway.get(f"/agents/{suffix}/did.json", headers=HOST).status_code == 410
    checked = verify(gateway, did=did)
    assert checked["verified"] is False and "the DID-LLM is revoked" in checked["reasons"]
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT actor_type, actor_id, data FROM audit_events WHERE subject_id = :s "
                "AND action = 'identity.status_changed'"
            ),
            {"s": issued["identity_id"]},
        ).one()
        revoked = connection.execute(
            text("SELECT revoked_at, revocation_reason FROM credentials WHERE identity_id = :s"),
            {"s": issued["identity_id"]},
        ).one()
    assert (row.actor_type, row.actor_id) == ("agent", AGENT_CONTROLLER)
    assert row.data["to"] == "revoked" and row.data["reason"] == "Key leaked"
    assert revoked.revoked_at is not None and revoked.revocation_reason == "Key leaked"
    after = gateway.post(f"/v1/dids/{did}/revocations", json={}, headers=HOST)
    assert after.status_code == 409
    nothing = gateway.post(
        f"/v1/dids/{did}/owner-changes",
        json={"new_controller_public_key": new_key()[1]},
        headers=HOST,
    )
    assert nothing.status_code == 409


# --------------------------------------------------------------------------- MCP


def tool(client: TestClient, name: str, **arguments: Any) -> dict[str, Any]:
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


@needs_db
def test_owners_change_and_revoke_over_mcp(gateway: TestClient) -> None:  # noqa: F811
    tools = rpc(gateway, "tools/list", {}).json()["result"]["tools"]
    annotations = {t["name"]: t.get("annotations", {}) for t in tools}
    for name in ("dmd_change_owner", "dmd_revoke_did"):
        assert annotations[name]["destructiveHint"] is True
        assert annotations[name]["readOnlyHint"] is False

    did = issue(gateway)["did"]
    key2, owner2 = new_key()
    change = tool(gateway, "dmd_change_owner", did=did, new_controller_public_key=owner2)
    assert change["step"] == "sign_owner_change" and "how_to_answer" in change
    approve, accept = item(change, "approve", AGENT_CONTROLLER), item(change, "accept", owner2)
    tool(
        gateway,
        "dmd_change_owner",
        did=did,
        change_id=change["change_id"],
        challenge_id=approve["challenge_id"],
        signature=sign_challenge(approve["message"]),
    )
    applied = tool(
        gateway,
        "dmd_change_owner",
        did=did,
        change_id=change["change_id"],
        challenge_id=accept["challenge_id"],
        signature=sign(key2, accept["message"]),
    )
    assert applied["status"] == "applied"
    status = tool(gateway, "dmd_change_owner", did=did, change_id=change["change_id"])
    assert status["status"] == "applied"
    half = rpc(
        gateway,
        "tools/call",
        {
            "name": "dmd_change_owner",
            "arguments": {
                "did": did,
                "change_id": change["change_id"],
                "challenge_id": accept["challenge_id"],
            },
        },
    ).json()["result"]
    assert half["isError"] is True

    asked = tool(gateway, "dmd_revoke_did", did=did, reason="Retired agent")
    assert asked["step"] == "sign_revocation" and asked["controller"] == owner2
    revoked = tool(
        gateway,
        "dmd_revoke_did",
        did=did,
        challenge_id=asked["challenge_id"],
        signature=sign(key2, asked["message"]),
    )
    assert revoked["status"] == "revoked"


# --------------------------------------------------------------------------- the owner-key tool


def _tool(client: TestClient) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "did_owner_key", REPO_ROOT / "scripts" / "did-owner-key.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.API = BASE

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        response = client.request(method, path, json=body, headers=HOST)
        return response.status_code, response.json()

    module.call = call
    return module


def _key_file(folder: Path, name: str, key: Ed25519PrivateKey) -> Path:
    path = folder / name
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@needs_db
def test_the_owner_key_tool_changes_owners_and_revokes(
    gateway: TestClient,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    did = issue(gateway)["did"]
    keys = tmp_path / "did-keys"
    keys.mkdir()
    _key_file(keys, "my-agent.key", AGENT_KEY)
    key2 = Ed25519PrivateKey.generate()
    new_file = _key_file(keys, "new.key", key2)
    cli = _tool(gateway)

    # Not confirmed: nothing signed.
    with pytest.raises(SystemExit):
        cli.change_owner(str(keys), did, [str(new_file)], confirm=lambda _: "no")
    assert record(gateway, did)["controller_public_key"] == AGENT_CONTROLLER

    cli.change_owner(str(keys), did, [str(new_file)], confirm=lambda _: "yes")
    out = capsys.readouterr().out
    assert "Done: the DID-LLM has its new owners." in out
    owner2 = cli.public_key(key2)
    assert record(gateway, did)["controller_public_key"] == owner2

    # A wallet joins: the tool signs the key's approval and asks for the wallet's signature.
    wallet = Account.create()

    def sign_wallet(prompt: str) -> str:
        out = capsys.readouterr().out
        start_ = out.index("DebitMyData owner change")
        message = "\n".join(line.strip() for line in out[start_:].strip().split("\n"))
        return wallet_signature(wallet, message)

    cli.change_owner(
        str(keys), did, [owner2, wallet.address], confirm=lambda _: "yes", paste=sign_wallet
    )
    now = record(gateway, did)
    assert now["controller_wallet"] == f"eip155:8453:{wallet.address}"

    # Another gateway's message is never signed.
    cli.API = "https://api.debitmydata.io"
    with pytest.raises(SystemExit) as stopped:
        cli.revoke(str(keys), did, confirm=lambda _: "REVOKE")
    assert "nothing was signed" in str(stopped.value)
    cli.API = BASE
    with pytest.raises(SystemExit):
        cli.revoke(str(keys), did, confirm=lambda _: "revoke")  # must be typed exactly
    assert record(gateway, did)["status"] == "active"
    cli.revoke(str(keys), did, confirm=lambda _: "REVOKE")
    assert "Revoked" in capsys.readouterr().out
    assert record(gateway, did)["status"] == "revoked"


def test_the_tool_reads_new_owners(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "did_owner_key", REPO_ROOT / "scripts" / "did-owner-key.py"
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    key = Ed25519PrivateKey.generate()
    path = _key_file(tmp_path, "new.key", key)
    address = Account.create().address
    assert cli.new_owners([str(path), address]) == {
        "new_controller_public_key": cli.public_key(key),
        "new_controller_wallet_address": address,
    }
    assert cli.new_owners([f"eip155:8453:{address}"]) == {
        "new_controller_wallet_address": f"eip155:8453:{address}"
    }
    for bad in (["nothing-here"], [address, Account.create().address]):
        with pytest.raises(SystemExit):
            cli.new_owners(bad)
    with pytest.raises(SystemExit):
        cli.did_path("not-a-did")
