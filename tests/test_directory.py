"""The DID-LLM directory, against PostgreSQL: owners list their DID-LLM with a signed profile,
change it and remove it the same way; the public API and pages show only listings that are
listed, active and not hidden; operators hide listings that break the rules.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from dmd_gateway.identity.wallets import WalletVerifier
from dmd_gateway.services import directory

from .conftest import TEST_DATABASE_URL
from .fakes import AGENT_CONTROLLER, sign_challenge
from .test_identity_status import issue, run
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import (  # noqa: F401
    ISSUER,
    answer,
    challenge,
    clean_chain,
    client,
    engine,
    quote,
    register,
    settle,
)
from .test_wallet_registration import NETWORK, wallet_signature

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

PROFILE = {
    "entity_type": "robot",
    "specialty": "Research assistant",
    "description": "Finds and summarizes papers on renewable energy, with sources for every claim.",
    "owner_name": "Ada Lovelace",
    "region": "California",
    "country": "USA",
    "created_on": "2025-03-14",
}


def ask(api: TestClient, did: str, **body: Any) -> Any:
    return api.post(f"/v1/directory/{did}/challenges", json=body)


def send(api: TestClient, did: str, challenge_id: str, signature: str) -> Any:
    return api.post(
        f"/v1/directory/{did}/proofs", json={"challenge_id": challenge_id, "signature": signature}
    )


def list_it(api: TestClient, did: str, **changes: Any) -> dict[str, Any]:
    asked = ask(api, did, listed=True, profile={**PROFILE, **changes})
    assert asked.status_code == 201, asked.text
    c = asked.json()
    proof = send(api, did, c["challenge_id"], sign_challenge(c["message"]))
    assert proof.status_code == 201, proof.text
    return proof.json()


def unlist(api: TestClient, did: str) -> dict[str, Any]:
    asked = ask(api, did, listed=False)
    assert asked.status_code == 201, asked.text
    c = asked.json()
    proof = send(api, did, c["challenge_id"], sign_challenge(c["message"]))
    assert proof.status_code == 201, proof.text
    return proof.json()


def names(api: TestClient, **params: Any) -> list[str]:
    response = api.get("/v1/directory", params=params)
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["entries"]]


def actions(db: Engine, identity_id: str) -> list[str]:
    with db.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT action FROM audit_events WHERE subject_id = :s "
                    "AND action LIKE 'directory.%' ORDER BY id"
                ),
                {"s": identity_id},
            ).scalars()
        )


@pytest.fixture(autouse=True)
def empty_directory(engine: Engine) -> None:  # noqa: F811
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM directory_listings"))


# --------------------------------------------------------------------------- listing


def test_the_owner_lists_a_did_llm_with_a_signed_profile(
    client: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(client)
    did, identity_id = issued["did"], issued["identity_id"]
    assert client.get(f"/v1/directory/{did}").status_code == 404  # nobody is listed unasked

    asked = ask(client, did, listed=True, profile=PROFILE)
    assert asked.status_code == 201, asked.text
    c = asked.json()
    assert c["did"] == did
    assert c["controller"] == AGENT_CONTROLLER
    assert c["listed"] is True and c["status"] == "open"
    assert c["signature_algorithm"] == "ed25519"
    lines = c["message"].split("\n")
    assert lines[:6] == [
        "DebitMyData directory listing",
        "Audience: http://localhost:8000",
        "Purpose: directory_listing",
        f"DID-LLM: {did}",
        f"Controller: {AGENT_CONTROLLER}",
        "Listed: yes (public)",
    ]
    assert lines[6:16] == [
        "Name: ResearchClaw-01",
        "Type: Robot",
        "Specialty: Research assistant",
        f"Description: {PROFILE['description']}",
        "Owner: Ada Lovelace",
        "Region: California",
        "Country: USA",
        "Created: 2025-03-14",
        "Terms: " + client.app.state.policy_pages["terms"].version.isoformat(),
        lines[15],
    ]
    assert re.fullmatch(r"Nonce: [\w-]{43}", lines[15])
    assert lines[16].startswith("Issued: ") and lines[17].startswith("Expires: ")
    # The same message again, by its ID, for signing tools.
    again = client.get(f"/v1/directory/challenges/{c['challenge_id']}").json()
    assert again["message"] == c["message"] and again["status"] == "open"
    assert client.get(f"/v1/directory/{did}").status_code == 404  # not before it is signed

    proof = send(client, did, c["challenge_id"], sign_challenge(c["message"]))
    assert proof.status_code == 201, proof.text
    body = proof.json()
    assert (body["status"], body["listed"], body["shown"]) == ("verified", True, True)
    assert client.get(f"/v1/directory/challenges/{c['challenge_id']}").json()["status"] == (
        "verified"
    )

    listing = client.get(f"/v1/directory/{did}")
    assert listing.status_code == 200
    assert listing.headers["access-control-allow-origin"] == "*"
    entry = listing.json()
    assert entry == body["entry"]
    assert {key: entry[key] for key in PROFILE} == PROFILE
    assert entry["name"] == "ResearchClaw-01"
    assert entry["identity_id"] == identity_id
    assert entry["entry_url"].endswith("/directory/" + identity_id.removeprefix("dmd_identity_"))
    assert client.get(f"/v1/directory/{identity_id}").json() == entry  # by identity ID too
    page = client.get("/v1/directory")
    assert page.headers["access-control-allow-origin"] == "*"
    assert page.json()["entries"] == [entry]
    assert actions(engine, identity_id) == ["directory.challenge_created", "directory.listed"]


def test_changes_and_removal_are_signed_too(
    client: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(client)
    did = issued["did"]
    first = list_it(client, did)["entry"]

    changed = list_it(client, did, specialty="Energy research", owner_name=None, region="")
    entry = changed["entry"]
    assert entry["specialty"] == "Energy research"
    assert entry["owner_name"] is None and entry["region"] is None
    assert entry["listed_at"] == first["listed_at"]  # listed since the first time

    removed = unlist(client, did)
    assert (removed["listed"], removed["shown"], removed["entry"]) == (False, False, None)
    assert client.get(f"/v1/directory/{did}").status_code == 404
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT listed, specialty, description, owner_name, listed_at FROM "
                "directory_listings WHERE identity_id = :i"
            ),
            {"i": issued["identity_id"]},
        ).one()
    assert tuple(row) == (False, None, None, None, None)  # the profile is gone
    gone = ask(client, did, listed=False)
    assert (gone.status_code, gone.json()["error"]["code"]) == (409, "not_listed")
    assert "Listed: no (remove it from the directory)" in unlist_message(client, did)

    again = list_it(client, did)["entry"]
    assert moment(again["listed_at"]) > moment(first["listed_at"])  # listed anew
    changes = [a for a in actions(engine, issued["identity_id"]) if not a.endswith("_created")]
    assert changes == [
        "directory.listed",
        "directory.updated",
        "directory.unlisted",
        "directory.listed",
        "directory.unlisted",
        "directory.listed",
    ]


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def unlist_message(api: TestClient, did: str) -> str:
    list_it(api, did)
    asked = ask(api, did, listed=False).json()
    send(api, did, asked["challenge_id"], sign_challenge(asked["message"]))
    return asked["message"]


def test_a_wrong_signature_changes_nothing_and_uses_up_the_message(
    client: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    did = issue(client)["did"]
    c = ask(client, did, listed=True, profile=PROFILE).json()
    forged = sign_challenge(c["message"].replace("Type: Robot", "Type: Avatar"))
    wrong = send(client, did, c["challenge_id"], forged)
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "invalid_signature"
    assert client.get(f"/v1/directory/{did}").status_code == 404
    assert client.get(f"/v1/directory/challenges/{c['challenge_id']}").json()["status"] == (
        "rejected"
    )
    replay = send(client, did, c["challenge_id"], sign_challenge(c["message"]))
    assert (replay.status_code, replay.json()["error"]["code"]) == (409, "challenge_already_used")

    # A registration's proof message is not a listing message.
    rid = quote(client)["registration_intent_id"]
    registration_message = challenge(client, rid)["message"]
    c = ask(client, did, listed=True, profile=PROFILE).json()
    assert send(
        client, did, c["challenge_id"], sign_challenge(registration_message)
    ).status_code == (422)

    # A malformed signature is not an answer: the message stays open.
    c = ask(client, did, listed=True, profile=PROFILE).json()
    bad = send(client, did, c["challenge_id"], "a" * 90)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_signature_encoding"
    assert client.get(f"/v1/directory/challenges/{c['challenge_id']}").json()["status"] == "open"

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE controller_challenges SET expires_at = now() - interval '1 second' "
                "WHERE id = :c"
            ),
            {"c": c["challenge_id"]},
        )
    late = send(client, did, c["challenge_id"], sign_challenge(c["message"]))
    assert (late.status_code, late.json()["error"]["code"]) == (410, "challenge_expired")
    assert client.get(f"/v1/directory/challenges/{c['challenge_id']}").json()["status"] == (
        "expired"
    )


def test_a_message_only_works_for_its_own_did_llm(client: TestClient) -> None:  # noqa: F811
    first, second = issue(client)["did"], issue(client)["did"]
    c = ask(client, first, listed=True, profile=PROFILE).json()
    other = send(client, second, c["challenge_id"], sign_challenge(c["message"]))
    assert (other.status_code, other.json()["error"]["code"]) == (404, "unknown_challenge")
    # Nor is a registration's challenge a listing message.
    rid = quote(client)["registration_intent_id"]
    registration_challenge = challenge(client, rid)
    assert (
        client.get(f"/v1/directory/challenges/{registration_challenge['challenge_id']}").status_code
        == 404
    )
    sent = send(client, first, registration_challenge["challenge_id"], "x" * 90)
    assert sent.status_code == 404


def test_open_messages_are_limited(client: TestClient) -> None:  # noqa: F811
    did = issue(client)["did"]
    for _ in range(directory.MAX_OPEN_CHALLENGES):
        assert ask(client, did, listed=True, profile=PROFILE).status_code == 201
    refused = ask(client, did, listed=True, profile=PROFILE)
    assert (refused.status_code, refused.json()["error"]["code"]) == (429, "too_many_challenges")


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"entity_type": "dragon"}, None),
        ({"specialty": "  "}, "specialty"),
        ({"specialty": "AI"}, "specialty"),
        ({"description": "Too short."}, "description"),
        ({"description": "word " * 101}, "description"),
        ({"description": "Visit https://example.com for more about this agent."}, "description"),
        ({"description": "Find us at www.example.com, we answer every question."}, "description"),
        ({"specialty": "Mail me: ada@example.com"}, "specialty"),
        ({"region": "Cali‮fornia"}, "region"),
        ({"owner_name": "DebitMyData Inc"}, "owner_name"),
        ({"owner_name": "Pаypal"}, "owner_name"),  # a Cyrillic "а"
        ({"created_on": "2999-01-01"}, "created_on"),
        ({"country": "x" * 61}, "country"),
    ],
)
def test_a_profile_that_breaks_the_rules_is_refused(
    client: TestClient,  # noqa: F811
    changes: dict[str, Any],
    field: str | None,
) -> None:
    did = issue(client)["did"]
    refused = ask(client, did, listed=True, profile={**PROFILE, **changes})
    assert refused.status_code == 422, refused.text
    if field is not None:
        error = refused.json()["error"]
        assert (error["code"], error["field"]) == ("invalid_profile", field)


def test_text_is_published_on_one_line_as_given(client: TestClient) -> None:  # noqa: F811
    did = issue(client)["did"]
    entry = list_it(
        client,
        did,
        specialty="  Customer\tsupport  ",
        description="Line one.\nLine two:   <b>not HTML</b> & “quotes” — fine.",
    )["entry"]
    assert entry["specialty"] == "Customer support"
    assert entry["description"] == "Line one. Line two: <b>not HTML</b> & “quotes” — fine."
    page = client.get(entry["entry_url"].replace("http://localhost:8000", ""))
    assert "&lt;b&gt;not HTML&lt;/b&gt; &amp; “quotes”" in page.text
    assert "<b>not HTML</b>" not in page.text


def test_only_active_did_llms_are_listed_and_shown(
    client: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(client)
    did = issued["did"]
    list_it(client, did)
    code, _ = run(engine, "did", "suspend", did, "--reason", "Under review", "--by", "Tyler")
    assert code == 0
    assert client.get(f"/v1/directory/{did}").status_code == 404
    assert names(client) == []
    refused = ask(client, did, listed=True, profile=PROFILE)
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "identity_not_active")
    run(engine, "did", "reactivate", did, "--reason", "Review done", "--by", "Tyler")
    assert client.get(f"/v1/directory/{did}").status_code == 200  # back with its DID-LLM


def test_operators_hide_a_listing_and_show_it_again(
    client: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(client)
    did = issued["did"]
    list_it(client, did)

    code, out = run(engine, "directory", "hide", did, "--reason", "Spam", "--by", "Tyler")
    assert code == 0, out
    assert out.startswith("Hidden: 'ResearchClaw-01'")
    assert client.get(f"/v1/directory/{did}").status_code == 404
    again = run(engine, "directory", "hide", did, "--reason", "Spam", "--by", "Tyler")
    assert again == (1, "This listing is already hidden: nothing was changed.")

    # The owner's changes are kept, but stay hidden.
    changed = list_it(client, did, specialty="Something else")
    assert (changed["listed"], changed["shown"], changed["entry"]) == (True, False, None)
    code, out = run(engine, "directory", "show", did)
    assert code == 0
    assert "hidden by Tyler" in out and "Spam" in out and "Something else" in out

    code, out = run(engine, "directory", "unhide", did, "--reason", "Fixed", "--by", "Tyler")
    assert code == 0, out
    assert client.get(f"/v1/directory/{did}").json()["specialty"] == "Something else"
    with engine.connect() as conn:
        operators = conn.execute(
            text(
                "SELECT action, actor_type, actor_id FROM audit_events WHERE subject_id = :s "
                "AND actor_type = 'operator' AND action LIKE 'directory.%' ORDER BY id"
            ),
            {"s": issued["identity_id"]},
        ).all()
    assert [tuple(row) for row in operators] == [
        ("directory.hidden", "operator", "Tyler"),
        ("directory.shown_again", "operator", "Tyler"),
    ]
    never = issue(client)["did"]
    assert run(engine, "directory", "hide", never, "--reason", "Spam", "--by", "Tyler") == (
        1,
        "This DID-LLM was never listed in the directory.",
    )
    assert run(engine, "directory", "show", never)[1].endswith("never listed")


def test_search_types_and_pages(client: TestClient) -> None:  # noqa: F811
    dids = [issue(client)["did"] for _ in range(3)]
    list_it(client, dids[0], entity_type="avatar", specialty="Customer support", country="France")
    list_it(client, dids[1], entity_type="robot", specialty="Warehouse logistics")
    list_it(client, dids[2], entity_type="sentient", specialty="Poetry_100% human")

    assert len(names(client)) == 3
    assert [e["specialty"] for e in client.get("/v1/directory").json()["entries"]] == [
        "Poetry_100% human",
        "Warehouse logistics",
        "Customer support",
    ]  # newest first
    assert [
        e["specialty"]
        for e in client.get("/v1/directory", params={"q": "support"}).json()["entries"]
    ] == ["Customer support"]
    assert len(names(client, q="france customer")) == 1  # every word, anywhere
    assert names(client, q="france logistics") == []
    assert len(names(client, q="100%")) == 1  # % and _ are plain characters
    assert len(names(client, q="y_100")) == 1
    assert names(client, q="o%h") == []
    assert len(names(client, type="robot")) == 1
    assert client.get("/v1/directory", params={"type": "dragon"}).status_code == 422

    first = client.get("/v1/directory", params={"limit": 2}).json()
    assert len(first["entries"]) == 2 and first["next_cursor"]
    second = client.get("/v1/directory", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [e["specialty"] for e in second["entries"]] == ["Customer support"]
    assert second["next_cursor"] is None
    bad = client.get("/v1/directory", params={"cursor": "bm9wZQ"})
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_cursor")


def test_a_wallet_owner_signs_with_the_wallet(client: TestClient) -> None:  # noqa: F811
    client.app.state.wallet_verifier = WalletVerifier({})
    owner = Account.create()
    q = quote(
        client,
        controller_public_key=None,
        controller_wallet_address=owner.address,
    )
    rid = q["registration_intent_id"]
    c = challenge(client, rid)
    assert (
        answer(client, rid, c["challenge_id"], wallet_signature(owner, c["message"])).status_code
        == 201
    )
    settle(client, q, txid=secrets.token_hex(32))
    did = register(client, rid).json()["did"]

    asked = ask(client, did, listed=True, profile=PROFILE).json()
    assert asked["controller"] == f"{NETWORK}:{owner.address}"
    assert asked["signature_algorithm"].startswith("eip191")
    other = Account.create()
    assert (
        send(
            client, did, asked["challenge_id"], wallet_signature(other, asked["message"])
        ).status_code
        == 422
    )
    asked = ask(client, did, listed=True, profile=PROFILE).json()
    proof = send(client, did, asked["challenge_id"], wallet_signature(owner, asked["message"]))
    assert proof.status_code == 201, proof.text
    assert proof.json()["shown"] is True
    no_key = ask(client, did, listed=True, profile=PROFILE, controller_type="ed25519")
    assert (no_key.status_code, no_key.json()["error"]["code"]) == (409, "no_such_controller")


# --------------------------------------------------------------------------- pages


def test_the_directory_pages(client: TestClient) -> None:  # noqa: F811
    empty = client.get("/directory")
    assert empty.status_code == 200
    assert "No DID-LLM is listed yet" in empty.text

    issued = issue(client)
    list_it(client, issued["did"])
    page = client.get("/directory")
    assert "ResearchClaw-01" in page.text and "Research assistant" in page.text
    assert "1 listing." in page.text
    assert 'aria-current=page href="/directory"' in page.text  # in the top bar
    assert "form-action 'self'" in page.headers["content-security-policy"]
    found = client.get("/directory", params={"q": "renewable", "type": "robot"})
    assert "ResearchClaw-01" in found.text and "1 listing found" in found.text
    nothing = client.get("/directory", params={"q": "nothing-like-this"})
    assert "No listing matches" in nothing.text
    assert client.get("/directory", params={"cursor": "garbage"}).status_code == 200

    suffix = issued["identity_id"].removeprefix("dmd_identity_")
    one = client.get(f"/directory/{suffix}")
    assert one.status_code == 200 and "Ada Lovelace" in one.text
    assert "mailto:info@debitmydata.com?subject=Directory%20listing%20report" in one.text
    assert client.get(f"/directory/{'0' * 26}").status_code == 404
    assert client.get("/directory/not-a-did").status_code == 404

    listing_page = client.get("/directory/list")
    assert listing_page.status_code == 200
    csp = listing_page.headers["content-security-policy"]
    assert "script-src 'sha256-" in csp and "connect-src 'self'" in csp
    assert "never asks for a private key" in listing_page.text
    # Off production, the tool's command names this gateway (DMD_GATEWAY_API=...).
    assert 'const PRODUCTION_API = "https://api.debitmydata.io";' in listing_page.text


def test_the_get_did_page_offers_the_directory(client: TestClient) -> None:  # noqa: F811
    page = client.get("/get-did").text
    assert 'const DIRECTORY_LIST = "/directory/list";' in page
    assert "List it in the directory" in page


def test_the_profile_rules_without_a_database() -> None:
    today = date(2026, 11, 12)
    profile = directory.clean_profile(
        {"entity_type": "Avatar", "specialty": " Guide ", "description": "A friendly guide " * 3},
        today=today,
    )
    assert profile.entity_type == "avatar" and profile.specialty == "Guide"
    assert profile.owner_name is None and profile.created_on is None
    with pytest.raises(directory.ProfileError, match="future"):
        directory.clean_profile({**PROFILE, "created_on": "2026-11-13"}, today=today)
    # The company's own controllers may name DebitMyData as the owner.
    directory.clean_profile({**PROFILE, "owner_name": "DebitMyData"}, today=today, brand_owner=True)
    now = datetime(2026, 11, 12, 9, tzinfo=UTC)
    removal = directory.listing_message(
        audience="https://api.debitmydata.io",
        did="did:web:api.debitmydata.io:agents:X",
        controller=AGENT_CONTROLLER,
        name="X",
        profile=None,
        terms_version="2026-11-12",
        nonce="n",
        issued_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    assert "Terms:" not in removal and "Listed: no (remove it from the directory)" in removal


def test_people_see_the_directory_on_the_apps_address(
    client: TestClient,  # noqa: F811
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(client)
    list_it(client, issued["did"])
    suffix = issued["identity_id"].removeprefix("dmd_identity_")
    app = _app_client(
        ready_registry,
        engine,
        issuer_key=ISSUER,
        settings_overrides={"app_host": "id.debitmydata.io"},
    )
    pages = ("/directory?q=renewable&type=robot", "/directory/list?did=x", f"/directory/{suffix}")
    for path in pages:
        moved = app.get(path, follow_redirects=False)
        assert moved.status_code == 308, path
        assert moved.headers["location"] == f"https://id.debitmydata.io{path}"
    there = app.get("/directory", headers={"Host": "id.debitmydata.io"})
    assert there.status_code == 200 and "ResearchClaw-01" in there.text
    # The API stays on the API's address, and links to the people's page.
    api = app.get(f"/v1/directory/{issued['did']}")
    assert api.status_code == 200
    assert api.json()["entry_url"] == f"https://id.debitmydata.io/directory/{suffix}"
    assert "People see it at https://id.debitmydata.io/directory" in app.get("/llms.txt").text
