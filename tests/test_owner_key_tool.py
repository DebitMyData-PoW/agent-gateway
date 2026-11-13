"""scripts/did-owner-key.py: the tool people use to own a DID-LLM with a key instead of a wallet.

Its keys and signatures are checked with the gateway's own code, so the tool cannot drift from
what the gateway accepts. The gateway itself is replaced by a stand-in for ``call``.
"""

from __future__ import annotations

import importlib.util
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from dmd_gateway.identity.crypto import decode_signature, parse_controller_key
from dmd_gateway.oauth.assertion import ASSERTION_TYPE, verify_assertion
from dmd_gateway.services import directory
from dmd_gateway.services.controller_proofs import challenge_message

from .conftest import REPO_ROOT

REGISTRATION = "dmd_did_intent_01M768SNM7P50G8GQYAYHWRCFH"


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "did_owner_key", REPO_ROOT / "scripts" / "did-owner-key.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_key(tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple:
    path = tmp_path / "owner.key"
    tool.new(str(path))
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    return path, printed


def test_a_new_key_is_one_the_gateway_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, printed = _new_key(tool, tmp_path, capsys)
    assert printed.startswith("ed25519:z6Mk")
    assert parse_controller_key(printed).canonical == printed
    assert printed == tool.public_key(tool.load(str(path)))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # readable by its owner only


def test_a_key_is_never_overwritten(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tool = _tool()
    path, _ = _new_key(tool, tmp_path, capsys)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        tool.new(str(path))
    assert path.read_bytes() == before


def _gateway(tool: ModuleType, controller: str, audience: str | None = None) -> list[tuple]:
    """Stands in for the gateway: answers the challenge, then accepts the proof."""
    now = datetime.now(UTC)
    message = challenge_message(
        audience=audience or tool.API,
        registration_intent_id=REGISTRATION,
        controller=controller,
        nonce="n0nce",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    calls: list[tuple] = []

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict[str, Any]]:
        calls.append((method, path, body))
        if path.endswith("/controller-challenges"):
            return 201, {
                "challenge_id": "dmd_challenge_X",
                "controller": controller,
                "message": message,
            }
        return 201, {"status": "verified", "next_action": "register_did"}

    tool.call = call
    calls.append(("message", message, None))
    return calls


def test_prove_signs_the_gateways_message_with_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, controller = _new_key(tool, tmp_path, capsys)
    calls = _gateway(tool, controller)
    tool.prove(str(path), REGISTRATION)
    message = calls[0][1]
    (_, challenge_path, _), (_, proof_path, proof) = calls[1], calls[2]
    assert challenge_path == f"/v1/dids/registrations/{REGISTRATION}/controller-challenges"
    assert proof_path == f"/v1/dids/registrations/{REGISTRATION}/controller-proofs"
    assert proof["challenge_id"] == "dmd_challenge_X"
    key = parse_controller_key(controller)
    assert key.verify(decode_signature(proof["signature"]), message.encode("utf-8"))
    assert "reload the Get a DID-LLM page and click Continue" in capsys.readouterr().out


def test_prove_never_signs_for_another_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, _ = _new_key(tool, tmp_path, capsys)
    calls = _gateway(tool, "ed25519:z6MkpCvDgxrfSmniB8DQeSdnzVAHAfrS1DQXHngrEzggvob8")
    with pytest.raises(SystemExit, match="not by this key"):
        tool.prove(str(path), REGISTRATION)
    assert [c[1] for c in calls if c[0] == "POST"][-1].endswith("/controller-challenges")


def test_prove_never_signs_a_message_for_another_gateway(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, controller = _new_key(tool, tmp_path, capsys)
    calls = _gateway(tool, controller, audience="https://evil.example")
    with pytest.raises(SystemExit, match="nothing was signed"):
        tool.prove(str(path), REGISTRATION)
    assert not any(c[1].endswith("/controller-proofs") for c in calls if c[0] == "POST")


def test_prove_checks_the_registration_id(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="dmd_did_intent_"):
        _tool().prove(str(tmp_path / "none.key"), "dmd_payment_intent_01M768SNM7P50G8GQYAYHWRCFH")


@pytest.mark.parametrize("api", ["http://api.debitmydata.io", "file:///etc/passwd", "ftp://x"])
def test_the_gateway_address_must_be_https(api: str) -> None:
    tool = _tool()
    tool.API = api
    with pytest.raises(SystemExit, match="must be an https:// address"):
        tool.call("GET", "/v1/discovery")


# --------------------------------------------------------------------------- directory listings

LISTING_CHALLENGE = "dmd_challenge_01M76A2B3C4D5E6F7G8H9JKMNP"
DID = "did:web:api.debitmydata.io:agents:01M7697G2GYM4KQNEPFPTTWPHF"


def _directory_gateway(
    tool: ModuleType,
    controller: str,
    *,
    audience: str | None = None,
    status: str = "open",
    did: str = DID,
) -> list[tuple]:
    """Stands in for the gateway: gives the listing message, then accepts the signature."""
    now = datetime.now(UTC)
    message = directory.listing_message(
        audience=audience or tool.API,
        did=did,
        controller=controller,
        name="ResearchClaw-01",
        profile=directory.Profile("robot", "Research assistant", "Finds papers. " * 3),
        terms_version="2026-11-12",
        nonce="n0nce",
        issued_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    calls: list[tuple] = [("message", message, None)]

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict[str, Any]]:
        calls.append((method, path, body))
        if method == "GET":
            return 200, {
                "challenge_id": LISTING_CHALLENGE,
                "did": DID,
                "status": status,
                "controller": controller,
                "message": message,
            }
        return 201, {"status": "verified", "listed": True, "shown": True}

    tool.call = call
    return calls


def test_directory_shows_signs_and_sends_the_listing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, controller = _new_key(tool, tmp_path, capsys)
    calls = _directory_gateway(tool, controller)
    tool.directory(str(path), LISTING_CHALLENGE, confirm=lambda _: "yes")
    message = calls[0][1]
    assert calls[1][:2] == ("GET", f"/v1/directory/challenges/{LISTING_CHALLENGE}")
    method, proof_path, proof = calls[2]
    assert (method, proof_path) == ("POST", f"/v1/directory/{DID}/proofs")
    assert proof["challenge_id"] == LISTING_CHALLENGE
    key = parse_controller_key(controller)
    assert key.verify(decode_signature(proof["signature"]), message.encode("utf-8"))
    out = capsys.readouterr().out
    assert "Specialty: Research assistant" in out  # the owner sees what is signed
    assert "Nonce:" not in out
    assert "the DID-LLM is in the directory" in out


def test_directory_signs_nothing_unless_confirmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, controller = _new_key(tool, tmp_path, capsys)
    calls = _directory_gateway(tool, controller)
    with pytest.raises(SystemExit, match="Not signed"):
        tool.directory(str(path), LISTING_CHALLENGE, confirm=lambda _: "")
    assert not any(c[0] == "POST" for c in calls)


@pytest.mark.parametrize(
    "gateway",
    [
        {"audience": "https://evil.example"},
        {"did": "did:web:api.debitmydata.io:agents:01M7697G2GYM4KQNEPFPTTWPHG"},
        {"status": "expired"},
    ],
)
def test_directory_never_signs_another_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], gateway: dict[str, str]
) -> None:
    tool = _tool()
    path, controller = _new_key(tool, tmp_path, capsys)
    calls = _directory_gateway(tool, controller, **gateway)
    with pytest.raises(SystemExit):
        tool.directory(str(path), LISTING_CHALLENGE, confirm=lambda _: "yes")
    assert not any(c[0] == "POST" for c in calls)


def test_directory_never_signs_for_another_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, _ = _new_key(tool, tmp_path, capsys)
    calls = _directory_gateway(tool, "ed25519:z6MkpCvDgxrfSmniB8DQeSdnzVAHAfrS1DQXHngrEzggvob8")
    with pytest.raises(SystemExit, match="not by this key"):
        tool.directory(str(path), LISTING_CHALLENGE, confirm=lambda _: "yes")
    assert not any(c[0] == "POST" for c in calls)


def test_a_message_missing_on_the_default_gateway_points_to_staging(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _tool()
    path, _ = _new_key(tool, tmp_path, capsys)
    monkeypatch.delenv("DMD_GATEWAY_API", raising=False)
    tool.call = lambda *_args, **_kwargs: (404, {"detail": "Not Found"})
    with pytest.raises(SystemExit, match="DMD_GATEWAY_API=https://staging-api.debitmydata.io"):
        tool.directory(str(path), LISTING_CHALLENGE, confirm=lambda _: "yes")
    with pytest.raises(SystemExit, match="This asked https://api.debitmydata.io"):
        tool.prove(str(path), REGISTRATION)
    # Already pointed at a gateway: no hint.
    monkeypatch.setenv("DMD_GATEWAY_API", "https://staging-api.debitmydata.io")
    with pytest.raises(SystemExit) as stopped:
        tool.directory(str(path), LISTING_CHALLENGE, confirm=lambda _: "yes")
    assert "This asked" not in str(stopped.value)


def test_a_folder_of_keys_signs_with_the_owner_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    keys = tmp_path / "did-keys"
    keys.mkdir()
    tool.new(str(keys / "other.key"))
    tool.new(str(keys / "owner.key"))
    (keys / "notes.txt").write_text("not a key")
    owner = capsys.readouterr().out.strip().splitlines()[-1]
    calls = _directory_gateway(tool, owner)
    tool.directory(str(keys), LISTING_CHALLENGE, confirm=lambda _: "yes")
    proof = calls[-1][2]
    key = parse_controller_key(owner)
    assert key.verify(decode_signature(proof["signature"]), calls[0][1].encode("utf-8"))
    assert f"The owner key is {keys / 'owner.key'}." in capsys.readouterr().out

    # The registration's proof too.
    calls = _gateway(tool, owner)
    tool.prove(str(keys), REGISTRATION)
    assert calls[-1][1].endswith("/controller-proofs")


def test_a_folder_without_the_owner_key_signs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    keys = tmp_path / "did-keys"
    keys.mkdir()
    tool.new(str(keys / "mine.key"))
    mine = capsys.readouterr().out.strip().splitlines()[-1]
    stranger = "ed25519:z6MkpCvDgxrfSmniB8DQeSdnzVAHAfrS1DQXHngrEzggvob8"
    calls = _directory_gateway(tool, stranger)
    with pytest.raises(SystemExit) as stopped:
        tool.directory(str(keys), LISTING_CHALLENGE, confirm=lambda _: "yes")
    text = str(stopped.value)
    assert f"its owner key is\n  {stranger}" in text
    assert f"mine.key  {mine}" in text and "Nothing was signed." in text
    assert not any(c[0] == "POST" for c in calls)
    with pytest.raises(SystemExit, match="No key file or folder"):
        tool.directory(str(tmp_path / "missing"), LISTING_CHALLENGE, confirm=lambda _: "yes")


# --------------------------------------------------------------------------- signing in (OAuth)

AGENT_DID = "did:web:api.debitmydata.io:agents:01M768SNM7P50G8GQYAYHWRCFH"
AUTH = "https://auth.debitmydata.io"
MCP = "https://api.debitmydata.io/agenticsuperhighway"


@pytest.mark.parametrize(
    ("api", "auth"),
    [
        ("https://api.debitmydata.io", "https://auth.debitmydata.io"),
        ("https://staging-api.debitmydata.io", "https://staging-auth.debitmydata.io"),
        ("http://localhost:8000", "http://localhost:8000"),
    ],
)
def test_the_authorization_server_is_next_to_the_gateway(api: str, auth: str) -> None:
    assert _tool().default_auth_server(api) == auth


def _auth_server(
    tool: ModuleType, controller: str | None, issuer: str = AUTH
) -> list[tuple[str, Any]]:
    """Stands in for the gateway (the DID document, discovery) and the authorization server."""
    tool.API, tool.AUTH = "https://api.debitmydata.io", AUTH
    seen: list[tuple[str, Any]] = []
    methods = (
        [{"type": "Multikey", "publicKeyMultibase": controller.removeprefix("ed25519:")}]
        if controller
        else [{"type": "BlockchainAccount", "blockchainAccountId": "eip155:8453:0xab"}]
    )

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict[str, Any]]:
        seen.append((path, body))
        if path == "/v1/discovery":
            return 200, {"mcp_url": MCP}
        return 200, {"id": AGENT_DID, "verificationMethod": methods}

    def fetch(url: str, form: dict | None = None) -> tuple[int, dict[str, Any]]:
        seen.append((url, form))
        if form is None:
            return 200, {"issuer": issuer, "token_endpoint": f"{issuer}/token"}
        return 200, {
            "access_token": "eyJ.the-token.sig",
            "token_type": "Bearer",
            "expires_in": 600,
            "scope": "did:read",
        }

    tool.call, tool.fetch = call, fetch
    return seen


def test_token_signs_in_with_the_owner_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, controller = _new_key(tool, tmp_path, capsys)
    seen = _auth_server(tool, controller)
    tool.token(str(path), AGENT_DID)
    out = capsys.readouterr().out
    assert f"Signed in as {AGENT_DID}." in out and "valid 10 minutes" in out
    assert "the-token" not in out  # only with --show
    assert seen[0][0] == "/agents/01M768SNM7P50G8GQYAYHWRCFH/did.json"
    url, form = seen[-1]
    assert url == f"{AUTH}/token"
    assert form["grant_type"] == "client_credentials"
    assert form["client_assertion_type"] == ASSERTION_TYPE
    assert form["resource"] == MCP and form["scope"] == "did:read"
    # The gateway's own check accepts what the tool signs.
    checked = verify_assertion(
        form["client_assertion"],
        audience=[f"{AUTH}/token", AUTH],
        controller_key=lambda did: controller,
        now=datetime.now(UTC),
    )
    assert checked.did == AGENT_DID

    tool.token(str(path), AGENT_DID, show=True)
    assert "eyJ.the-token.sig" in capsys.readouterr().out


def test_token_signs_nothing_when_something_is_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _tool()
    path, controller = _new_key(tool, tmp_path, capsys)
    stranger = "ed25519:z6MkpCvDgxrfSmniB8DQeSdnzVAHAfrS1DQXHngrEzggvob8"
    for owner, issuer, words in (
        (stranger, AUTH, "not by this key"),
        (None, AUTH, "controlled by a wallet"),
        (controller, "https://auth.elsewhere.example", "does not describe itself"),
    ):
        seen = _auth_server(tool, owner, issuer=issuer)
        with pytest.raises(SystemExit, match=words):
            tool.token(str(path), AGENT_DID)
        assert not any(form for _, form in seen if isinstance(form, dict) and "grant_type" in form)
    _auth_server(tool, controller)
    with pytest.raises(SystemExit, match="DMD_GATEWAY_API=https://staging-api.debitmydata.io"):
        tool.token(
            str(path), "did:web:staging-api.debitmydata.io:agents:01M768SNM7P50G8GQYAYHWRCFH"
        )
    with pytest.raises(SystemExit, match="looks like did:web"):
        tool.token(str(path), "dmd_identity_01M768SNM7P50G8GQYAYHWRCFH")
