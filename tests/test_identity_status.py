"""Suspending, reactivating and revoking a DID with dmd-gateway-admin (ID-13), against
PostgreSQL: what the public API answers at each step, what the audit trail keeps, and what is
refused without changing anything.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from dmd_gateway import admin
from dmd_gateway.db.session import session_factory

from .conftest import TEST_DATABASE_URL
from .fakes import AGENT_CONTROLLER
from .test_quotes import ready_registry  # noqa: F401
from .test_registration import (  # noqa: F401
    clean_chain,
    client,
    engine,
    prove,
    quote,
    register,
    registration,
    settle,
)

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
REASON = "Reported impersonation, under review"


def issue(api: TestClient) -> dict[str, Any]:
    q = quote(api)
    rid = q["registration_intent_id"]
    prove(api, rid)
    settle(api, q, txid=os.urandom(32).hex())
    response = register(api, rid)
    assert response.status_code == 201, response.text
    return response.json()


def run(db: Engine, *argv: str, ask: Callable[[str], str] = lambda _: "") -> tuple[int, str]:
    lines: list[str] = []
    code = admin.run(
        admin.parser().parse_args(list(argv)),
        session_factory(db),
        out=lines.append,
        ask=ask,
        now=NOW,
    )
    return code, "\n".join(lines)


def changes(db: Engine, identity_id: str) -> list[Any]:
    with db.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, actor_type, actor_id, data FROM audit_events"
                    " WHERE action = 'identity.status_changed' AND subject_id = :s ORDER BY id"
                ),
                {"s": identity_id},
            )
        )


def test_suspend_reactivate_then_revoke(client: TestClient, engine: Engine) -> None:  # noqa: F811
    issued = issue(client)
    did, iid, rid = issued["did"], issued["identity_id"], issued["registration_intent_id"]
    suffix, credential = iid.removeprefix("dmd_identity_"), issued["credential_id"]
    assert issued["did_status"] == "active"

    # Suspend, naming the DID itself.
    code, out = run(engine, "did", "suspend", did, "--reason", REASON, "--by", "Tyler")
    assert code == 0, out
    assert out.splitlines()[0] == f"Suspended: 'ResearchClaw-01' ({did})"
    assert "was active, now suspended; entitlements changed: 1; credentials revoked: 0" in out
    gone = client.get(f"/agents/{suffix}/did.json")
    assert gone.status_code == 410
    assert gone.json()["error"]["code"] == "deactivated"
    record = client.get(f"/v1/dids/{iid}").json()
    assert (record["status"], record["entitlements"]) == ("suspended", [])
    assert client.get(f"/v1/credentials/{credential}").json()["status"] == "suspended"
    rows = client.get(f"/v1/entitlements/{iid}").json()["entitlements"]
    assert [(e["entitlement"], e["status"]) for e in rows] == [("dmd_did_holder", "suspended")]
    assert registration(client, rid)["did_status"] == "suspended"
    assert register(client, rid).json()["did_status"] == "suspended"

    # Reactivate, naming the identity ID: everything is back.
    code, out = run(
        engine, "did", "reactivate", iid, "--reason", "No misuse found", "--by", "Tyler"
    )
    assert code == 0, out
    assert client.get(f"/agents/{suffix}/did.json").status_code == 200
    assert client.get(f"/v1/dids/{iid}").json()["entitlements"] == ["dmd_did_holder"]
    assert client.get(f"/v1/credentials/{credential}").json()["status"] == "active"
    assert registration(client, rid)["did_status"] == "active"

    # Revoke, naming the registration: a wrong confirmation changes nothing.
    code, out = run(
        engine,
        "did",
        "revoke",
        rid,
        "--reason",
        "Fraud confirmed",
        "--by",
        "Tyler",
        ask=lambda _: "x",
    )
    assert (code, out.splitlines()[-1]) == (1, "Not confirmed: nothing was changed.")
    assert client.get(f"/v1/dids/{iid}").json()["status"] == "active"
    code, out = run(
        engine,
        "did",
        "revoke",
        rid,
        "--reason",
        "Fraud confirmed",
        "--by",
        "Tyler",
        ask=lambda prompt: iid[-6:] if iid[-6:] in prompt else "",
    )
    assert code == 0, out
    assert "credentials revoked: 1" in out
    stored = client.get(f"/v1/credentials/{credential}").json()
    assert stored["status"] == "revoked"
    assert stored["revoked_at"] is not None
    rows = client.get(f"/v1/entitlements/{iid}").json()["entitlements"]
    assert [e["status"] for e in rows] == ["revoked"]
    assert client.get(f"/agents/{suffix}/did.json").status_code == 410

    # A revoked DID never comes back.
    code, out = run(engine, "did", "reactivate", did, "--reason", "Changed our mind", "--by", "T")
    assert (code, out) == (1, "A revoked DID-LLM cannot become active: nothing was changed.")
    code, out = run(engine, "did", "suspend", did, "--reason", "Changed our mind", "--by", "T")
    assert (code, out) == (1, "A revoked DID-LLM cannot become suspended: nothing was changed.")

    # The whole history is in the audit trail, with who and why, and it cannot be rewritten.
    history = changes(engine, iid)
    assert [(r.data["from"], r.data["to"]) for r in history] == [
        ("active", "suspended"),
        ("suspended", "active"),
        ("active", "revoked"),
    ]
    assert {(r.actor_type, r.actor_id) for r in history} == {("operator", "Tyler")}
    assert history[0].data == {"did": did, "from": "active", "to": "suspended", "reason": REASON}
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_events SET data = '{}' WHERE id = :i"), {"i": history[0].id}
        )

    # `show` gives everything on one screen, by any name of the DID (here its did.json address).
    code, out = run(engine, "did", "show", f"http://localhost:8000/agents/{suffix}/did.json")
    assert code == 0
    assert "Status        revoked" in out
    assert "Entitlements  dmd_did_holder (revoked)" in out
    assert f"Credentials   {credential} (revoked)" in out
    assert f"suspended (was active) by Tyler: {REASON}" in out
    assert "revoked (was active) by Tyler: Fraud confirmed" in out
    assert "identity.issued (agent)" in out


def test_refused_changes_write_nothing(client: TestClient, engine: Engine) -> None:  # noqa: F811
    issued = issue(client)
    did, iid = issued["did"], issued["identity_id"]
    unknown = "did:web:localhost%3A8000:agents:" + "0" * 26

    for argv, message in (
        (("suspend", did, "--reason", "x", "--by", "Tyler"), "Give a reason of 3 to 300"),
        (("suspend", did, "--reason", "y" * 301, "--by", "Tyler"), "Give a reason of 3 to 300"),
        (("suspend", did, "--reason", REASON, "--by", "$(whoami)"), "Give your name with --by"),
        (("suspend", unknown, "--reason", REASON, "--by", "Tyler"), "No DID-LLM found"),
        (("reactivate", did, "--reason", REASON, "--by", "Tyler"), "already active"),
    ):
        code, out = run(engine, "did", *argv)
        assert code == 1 and message in out, (argv, out)

    def no_terminal(_: str) -> str:
        raise EOFError

    code, out = run(engine, "did", "revoke", did, "--reason", REASON, "--by", "T", ask=no_terminal)
    assert (code, out.splitlines()[-1]) == (1, "Not confirmed: nothing was changed.")
    assert changes(engine, iid) == []
    assert client.get(f"/v1/dids/{iid}").json()["status"] == "active"

    # --yes skips the question (for scripts); the reason is kept on one line.
    code, out = run(
        engine, "did", "revoke", did, "--reason", "Asked by\nthe holder", "--by", "T", "--yes"
    )
    assert code == 0, out
    (only,) = changes(engine, iid)
    assert only.data["reason"] == "Asked by the holder"


def test_reason_and_operator_are_required() -> None:
    for argv in (
        ["did", "suspend", "did:web:x", "--by", "Tyler"],
        ["did", "suspend", "did:web:x", "--reason", "Some reason"],
        ["did", "delete", "did:web:x"],
    ):
        with pytest.raises(SystemExit):
            admin.parser().parse_args(argv)


def test_names_check_lists_issued_names_a_quote_would_refuse(
    client: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(client)
    iid = issued["identity_id"]
    # A name issued before the check existed (set here directly, as the quote now refuses it).
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE identities SET display_name = 'DebitMyData Support' WHERE id = :i"),
            {"i": iid},
        )
    lines: list[str] = []
    args = admin.parser().parse_args(["names", "check"])
    assert admin.run(args, session_factory(engine), out=lines.append) == 0
    report = "\n".join(lines)
    assert "would be refused today" in report
    assert f"{iid} (active) 'DebitMyData Support'" in report
    assert "looks like one of DebitMyData's own names" in report

    # The same name is fine when the DID's controller is one of the company's.
    lines.clear()
    owners = frozenset({AGENT_CONTROLLER})
    assert admin.run(args, session_factory(engine), out=lines.append, brand=owners) == 0
    assert iid not in "\n".join(lines)
