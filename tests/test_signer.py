"""The separate credential signer (ID-15): the API signs through it and never holds the key.

The signer runs here on a real Unix socket against the test database. Covers: a DID and a
donation receipt signed through it, its own checks of the database (nothing signed for an
unpaid registration, nothing signed twice), the API refusing a signature from another key,
and what agents see while the signer is down.
"""

from __future__ import annotations

import socket
import tempfile
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import verify_proof
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.identity.issuer import IssuerKey
from dmd_gateway.settings import Settings
from dmd_gateway.signer import policy
from dmd_gateway.signer.client import RemoteIssuer, SignerUnavailable
from dmd_gateway.signer.service import Signer, build, serve
from dmd_gateway.signer.wire import MAX_MESSAGE_BYTES, encode

from .conftest import TEST_DATABASE_URL
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import (  # noqa: F401
    clean_chain,
    engine,
    prove,
    quote,
    register,
    scalar,
    settle,
)

BASE = "http://localhost:8000"
DID = issuer_did(BASE)
KEY = IssuerKey(Ed25519PrivateKey.generate(), DID)

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def _running(signer: Signer) -> tuple[Path, Any]:
    folder = Path(tempfile.mkdtemp(prefix="dmd-signer-"))  # short: socket paths are limited
    path = folder / "signer.sock"
    server = serve(signer, path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return path, server


@pytest.fixture
def signer_socket(engine: Engine) -> Iterator[Path]:  # noqa: F811
    path, server = _running(Signer(key=KEY, sessions=sessionmaker(engine)))
    yield path
    server.shutdown()
    server.server_close()


def remote(path: Path, key: IssuerKey = KEY) -> RemoteIssuer:
    return RemoteIssuer(socket_path=path, did=DID, public_key_multibase=key.public_key_multibase)


# --------------------------------------------------------------------------- no database


def _document(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "@context": ["https://www.w3.org/ns/credentials/v2"],
        "type": ["VerifiableCredential", "DebitMyDataRegistrationCredential"],
        "issuer": {"id": DID, "name": "DebitMyData"},
        "credentialSubject": {},
    }
    document.update(changes)
    return document


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"issuer": {"id": "did:web:evil.example"}}, "another issuer"),
        ({"type": ["VerifiableCredential", "SomethingElse"]}, "only DID registration"),
        ({"@context": ["https://evil.example"]}, "@context"),
        ({"proof": {}}, "already signed"),
    ],
)
def test_only_this_issuers_credentials_and_receipts(changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(policy.Refused, match=reason):
        policy.kind_of(_document(**changes), DID)
    assert policy.kind_of(_document(), DID) == "registration_credential"


def test_settings_need_the_socket_and_its_public_key_together(tmp_path: Path) -> None:
    common: dict[str, Any] = {"_env_file": None}
    with pytest.raises(ValueError, match="go together"):
        Settings(**common, issuer_signer_socket=tmp_path / "s.sock")
    with pytest.raises(ValueError, match="Multikey"):
        Settings(**common, issuer_signer_socket=tmp_path / "s.sock", issuer_public_key="abc")
    with pytest.raises(ValueError, match="not both"):
        Settings(
            **common,
            issuer_signer_socket=tmp_path / "s.sock",
            issuer_public_key=KEY.public_key_multibase,
            issuer_key_path=tmp_path / "k.pem",
        )
    production = Settings(
        **common,
        environment="production",
        database_url="postgresql+psycopg://gateway:x@127.0.0.1/agent_gateway",
        public_base_url="https://api.example.com",
        issuer_signer_socket=tmp_path / "s.sock",
        issuer_public_key=KEY.public_key_multibase,
    )
    assert production.issuer_signer_socket == tmp_path / "s.sock"


def test_the_signer_refuses_to_start_without_its_settings(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing settings"):
        build({"DMD_SIGNER_SOCKET": str(tmp_path / "s.sock")})


def test_an_unreachable_signer_signs_nothing(tmp_path: Path) -> None:
    issuer = remote(tmp_path / "nobody.sock")
    assert issuer.ready() is False
    with pytest.raises(ApiError) as error:
        issuer.sign(_document(), created=datetime.now(UTC))
    assert error.value.status_code == 503 and error.value.code == "issuance_unavailable"


def test_an_oversized_request_is_refused() -> None:
    signer = Signer(key=KEY, sessions=None)  # type: ignore[arg-type]  # never reaches the DB
    path, server = _running(signer)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(path))
            sock.sendall(b"x" * (MAX_MESSAGE_BYTES + 10) + b"\n")
            answer = sock.recv(4096)
        assert b'"ok":false' in answer
        # The socket is not world-writable.
        assert path.stat().st_mode & 0o007 == 0
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(path))
            sock.sendall(encode({"op": "describe"}))
            assert b'"ok":true' in sock.recv(4096)
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- with the database


@needs_db
def test_a_did_is_issued_through_the_signer(
    signer_socket: Path,
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    issuer = remote(signer_socket)
    with _app_client(ready_registry, engine, issuer_key=issuer) as client:
        assert client.get("/readyz").json()["checks"]["credential_signer"] == "ok"
        issuer_doc = client.get("/.well-known/did.json").json()
        assert issuer_doc["verificationMethod"][0]["publicKeyMultibase"] == (
            KEY.public_key_multibase
        )

        q = quote(client)
        rid = q["registration_intent_id"]
        # Before payment and proof, the signer itself refuses, whatever the API asks.
        forged = _forged_credential(rid, q["payment_intent_id"])
        with pytest.raises(ApiError):
            issuer.sign(forged, created=datetime.now(UTC))

        settle(client, q, txid="a1" * 32)
        prove(client, rid)
        response = register(client, rid)
        assert response.status_code == 201, response.text
        credential = response.json()["credential"]
        assert verify_proof(credential, KEY.public_key_multibase)
        assert credential["proof"]["verificationMethod"] == f"{DID}#{KEY.public_key_multibase}"

        # Never a second credential for the same registration, even if the API asks.
        with pytest.raises(SignerUnavailable, match="refused: the registration is issued"):
            issuer._sign(_forged_credential(rid, q["payment_intent_id"]), datetime.now(UTC))


def _forged_credential(rid: str, pid: str) -> dict[str, Any]:
    return _document(
        credentialSubject={
            "id": f"{DID}:agents:forged",
            "displayName": "ResearchClaw-01",
            "participantType": "autonomous_agent",
            "registration": {"registrationIntentId": rid, "payment": {"paymentIntentId": pid}},
        }
    )


@needs_db
def test_a_signature_from_another_key_is_never_used(
    signer_socket: Path,
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    expected = IssuerKey(Ed25519PrivateKey.generate(), DID)  # not the signer's key
    with _app_client(ready_registry, engine, issuer_key=remote(signer_socket, expected)) as client:
        assert client.get("/readyz").status_code == 503
        q = quote(client)
        rid = q["registration_intent_id"]
        settle(client, q, txid="b2" * 32)
        prove(client, rid)
        response = register(client, rid)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "issuance_unavailable"
        assert response.headers["Retry-After"] == "60"
        issued = "SELECT count(*) FROM identities WHERE registration_intent_id = :r"
        assert scalar(engine, issued, r=rid) == 0


@needs_db
def test_while_the_signer_is_down_nothing_is_issued_then_it_resumes(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    folder = Path(tempfile.mkdtemp(prefix="dmd-signer-"))
    issuer = RemoteIssuer(
        socket_path=folder / "signer.sock", did=DID, public_key_multibase=KEY.public_key_multibase
    )
    with _app_client(ready_registry, engine, issuer_key=issuer) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 503 and ready.json()["checks"]["credential_signer"] == (
            "unavailable"
        )
        q = quote(client, display_name="SignerDown-01")
        rid = q["registration_intent_id"]
        settle(client, q, txid="cd" * 32)
        prove(client, rid)
        assert register(client, rid).status_code == 503

        server = serve(Signer(key=KEY, sessions=sessionmaker(engine)), folder / "signer.sock")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            again = register(client, rid)
            assert again.status_code == 201, again.text
            assert verify_proof(again.json()["credential"], KEY.public_key_multibase)
        finally:
            server.shutdown()
            server.server_close()


@needs_db
def test_a_donation_receipt_is_signed_through_the_signer(
    signer_socket: Path,
    engine: Engine,  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    import yaml

    from .conftest import REGISTRY_PATH
    from .test_donations import PRICE_SOURCE, TestPayer, donate, make_app, required_for
    from .x402_fakes import NETWORK, TOKEN, reset_chain

    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    data["rails"]["dmd_pow_native"]["price_policy"] = {
        "source": PRICE_SOURCE,
        "max_price_age_seconds": 300,
        "max_deviation_bps": 200,
        "fallback": "refuse_quote",
    }
    data["rails"]["x402_usdc_base"]["asset"]["canonical_id"] = f"{NETWORK}/erc20:{TOKEN}"
    routes = tmp_path_factory.mktemp("signer") / "routes.yaml"
    routes.write_text(yaml.safe_dump(data), encoding="utf-8")
    reset_chain()
    with TestClient(make_app(routes, engine, issuer=remote(signer_socket))) as client:
        required = required_for(client, "5.00")
        response = donate(client, {"amount_usd": "5.00"}, TestPayer().header(required))
        assert response.status_code == 201, response.text
        receipt = response.json()["receipt"]
        assert verify_proof(receipt, KEY.public_key_multibase)
        assert receipt["type"] == ["VerifiableCredential", "DebitMyDataDonationReceipt"]
    reset_chain()
