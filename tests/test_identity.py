"""Keys, signatures, DIDs and credentials (no database needed)."""

from __future__ import annotations

import base64
import copy
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from dmd_gateway.identity import crypto
from dmd_gateway.identity.credentials import RegistrationFacts, registration_credential
from dmd_gateway.identity.did import (
    agent_did,
    agent_did_document,
    did_document_url,
    did_web_host,
    issuer_did,
    issuer_did_document,
)
from dmd_gateway.identity.issuer import IssuerKey, IssuerKeyError, load_issuer_key, main
from dmd_gateway.services.controller_proofs import challenge_message
from dmd_gateway.settings import Settings

from .fakes import AGENT_CONTROLLER, AGENT_KEY, AGENT_PUBLIC_HEX, sign_challenge

P = 2**255 - 19
NOW = datetime(2026, 10, 19, 18, 0, 0, tzinfo=UTC)

# --------------------------------------------------------------------------- encodings


def test_base58_known_values() -> None:
    assert crypto.b58encode(b"hello world") == "StV1DL6CwTryKyV"
    assert crypto.b58encode(b"\x00\x00\x01") == "112"
    assert crypto.b58decode("112") == b"\x00\x00\x01"
    for data in (b"\x00", b"\x00\xff", os.urandom(34)):
        assert crypto.b58decode(crypto.b58encode(data)) == data


def test_base58_rejects_bad_characters() -> None:
    with pytest.raises(crypto.EncodingError):
        crypto.b58decode("0OIl")


def test_rfc8032_vector_and_multikey_form() -> None:
    # RFC 8032, section 7.1, test 1.
    signature = AGENT_KEY.sign(b"")
    assert signature.hex() == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
        "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    key = crypto.parse_controller_key(f"ed25519:{AGENT_PUBLIC_HEX}")
    assert key.raw.hex() == AGENT_PUBLIC_HEX
    assert key.canonical == AGENT_CONTROLLER
    assert key.multibase.startswith("z6Mk")
    assert key.verify(signature, b"")
    assert not key.verify(signature, b"x")


def test_controller_key_encodings_are_equivalent() -> None:
    raw = bytes.fromhex(AGENT_PUBLIC_HEX)
    forms = [
        AGENT_CONTROLLER,
        f"ed25519:{AGENT_PUBLIC_HEX}",
        "ed25519:" + base64.b64encode(raw).decode(),
        "ed25519:" + base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "ED25519:" + AGENT_PUBLIC_HEX.upper(),
    ]
    assert {crypto.parse_controller_key(form).canonical for form in forms} == {AGENT_CONTROLLER}


@pytest.mark.parametrize(
    "value",
    [
        "z6MktwupdmLXVVqTzCw4i46r4uGyosGXRnR3XjN4Zq7oMMsw",  # no key type
        "secp256k1:" + "02" + "11" * 32,  # unsupported type
        "ed25519:" + "11" * 31,  # too short
        "ed25519:" + "zz" * 32,  # not hex, not base64, not multibase
        "ed25519:z6Mk0000",  # invalid base58
        # Weak keys: small-order points and a non-canonical encoding.
        "ed25519:01" + "00" * 31,  # identity
        "ed25519:" + (P - 1).to_bytes(32, "little").hex(),  # order 2
        "ed25519:" + "00" * 32,  # order 4
        "ed25519:26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",  # order 8
        "ed25519:" + (P + 1).to_bytes(32, "little").hex(),  # y >= p
    ],
)
def test_invalid_or_weak_controller_keys_are_refused(value: str) -> None:
    with pytest.raises(crypto.ControllerKeyError):
        crypto.parse_controller_key(value)


def test_random_keys_are_accepted() -> None:
    for _ in range(50):
        public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        assert crypto.is_strong_ed25519_public_key(public)


def test_signature_encodings() -> None:
    signature = AGENT_KEY.sign(b"message")
    for text in (
        signature.hex(),
        base64.b64encode(signature).decode(),
        base64.urlsafe_b64encode(signature).decode().rstrip("="),
        crypto.encode_signature(signature),
    ):
        assert crypto.decode_signature(text) == signature
    for bad in ("", "ab" * 63, "!" * 86, base64.b64encode(signature[:63]).decode()):
        with pytest.raises(crypto.EncodingError):
            crypto.decode_signature(bad)


# --------------------------------------------------------------------------- JCS


def test_canonical_json_follows_rfc8785() -> None:
    value = {"b": [1, "é", None, True, False], "a": {"z": "\n", "\u20ac": 1, "\U0001f600": 2}}
    expected = '{"a":{"z":"\\n","\u20ac":1,"\U0001f600":2},"b":[1,"é",null,true,false]}'
    assert crypto.canonical_json(value) == expected.encode()
    # Sorting is by UTF-16 code units: U+FB33 sorts after the surrogate pair of U+1F600.
    assert crypto.canonical_json({"\ufb33": 1, "\U0001f600": 2}).decode() == (
        '{"\U0001f600":2,"\ufb33":1}'
    )


@pytest.mark.parametrize("value", [{"a": 1.5}, {1: "a"}, {"a": 2**60}, {"a": object()}])
def test_canonical_json_refuses_ambiguous_values(value: object) -> None:
    with pytest.raises(ValueError):
        crypto.canonical_json(value)


# --------------------------------------------------------------------------- Data Integrity


def _issuer() -> IssuerKey:
    return IssuerKey(Ed25519PrivateKey.generate(), issuer_did("https://api.debitmydata.com"))


def test_eddsa_jcs_2022_proof_matches_the_specification() -> None:
    issuer = _issuer()
    document = {"@context": ["https://www.w3.org/ns/credentials/v2"], "id": "urn:x", "a": "b"}
    signed = crypto.add_proof(
        document, issuer.private_key, verification_method=issuer.verification_method, created=NOW
    )
    proof = signed["proof"]
    assert proof == {
        "type": "DataIntegrityProof",
        "cryptosuite": "eddsa-jcs-2022",
        "created": "2026-10-19T18:00:00Z",
        "verificationMethod": issuer.verification_method,
        "proofPurpose": "assertionMethod",
        "proofValue": proof["proofValue"],
    }
    # Recompute the signed bytes exactly as the cryptosuite defines them.
    config = {k: v for k, v in proof.items() if k != "proofValue"}
    config["@context"] = document["@context"]
    hash_data = (
        hashlib.sha256(crypto.canonical_json(config)).digest()
        + hashlib.sha256(crypto.canonical_json(document)).digest()
    )
    issuer.private_key.public_key().verify(crypto.multibase_decode(proof["proofValue"]), hash_data)
    assert crypto.verify_proof(signed, issuer.public_key_multibase)


def test_tampered_or_foreign_credentials_fail_verification() -> None:
    issuer = _issuer()
    document = {"@context": ["https://www.w3.org/ns/credentials/v2"], "credentialSubject": {"a": 1}}
    signed = crypto.add_proof(
        document, issuer.private_key, verification_method=issuer.verification_method, created=NOW
    )
    assert crypto.verify_proof(signed, issuer.public_key_multibase)

    tampered = copy.deepcopy(signed)
    tampered["credentialSubject"]["a"] = 2
    assert not crypto.verify_proof(tampered, issuer.public_key_multibase)

    redated = copy.deepcopy(signed)
    redated["proof"]["created"] = "2030-01-01T00:00:00Z"
    assert not crypto.verify_proof(redated, issuer.public_key_multibase)

    assert not crypto.verify_proof(signed, _issuer().public_key_multibase)
    assert not crypto.verify_proof({k: v for k, v in signed.items() if k != "proof"}, "z6Mk")


# --------------------------------------------------------------------------- DIDs


def test_did_web_identifiers() -> None:
    assert did_web_host("https://API.debitmydata.com") == "api.debitmydata.com"
    assert did_web_host("http://localhost:8000") == "localhost%3A8000"
    assert issuer_did("https://api.debitmydata.com") == "did:web:api.debitmydata.com"
    ident = "dmd_identity_01JA8Z3T6H5K9V2M4Q7R8S0W1X"
    assert agent_did("https://api.debitmydata.com", ident) == (
        "did:web:api.debitmydata.com:agents:01JA8Z3T6H5K9V2M4Q7R8S0W1X"
    )
    # did:web resolution: replace ':' with '/', decode %3A, add /did.json.
    assert did_document_url("http://localhost:8000", ident) == (
        "http://localhost:8000/agents/01JA8Z3T6H5K9V2M4Q7R8S0W1X/did.json"
    )


def test_did_documents() -> None:
    key = crypto.parse_controller_key(AGENT_CONTROLLER)
    did = "did:web:api.debitmydata.com:agents:01JA8Z3T6H5K9V2M4Q7R8S0W1X"
    document = agent_did_document(did, key, record_url="https://api.debitmydata.com/v1/dids/x")
    (method,) = document["verificationMethod"]
    assert method == {
        "id": f"{did}#{key.multibase}",
        "type": "Multikey",
        "controller": did,
        "publicKeyMultibase": key.multibase,
    }
    assert document["authentication"] == [method["id"]] == document["assertionMethod"]
    issuer = issuer_did_document("did:web:api.debitmydata.com", key.multibase)
    assert issuer["assertionMethod"] == [f"did:web:api.debitmydata.com#{key.multibase}"]


def test_registration_credential_is_signed_and_names_the_payment() -> None:
    issuer = _issuer()
    did = agent_did("https://api.debitmydata.com", "dmd_identity_01JA8Z3T6H5K9V2M4Q7R8S0W1X")
    credential = registration_credential(
        RegistrationFacts(
            credential_url="https://api.debitmydata.com/v1/credentials/dmd_credential_X",
            subject_did=did,
            display_name="ResearchClaw-01",
            participant_type="autonomous_agent",
            controller=crypto.parse_controller_key(AGENT_CONTROLLER),
            controller_verified_at=NOW,
            registration_intent_id="dmd_did_intent_X",
            payment_intent_id="dmd_payment_intent_X",
            service_title="DebitMyData Agentic DID Registration",
            commercial_currency="USD",
            commercial_amount="25.00",
            payment_option="dmd_pow_native",
            asset="DMD",
            network="dmd:mainnet",
            amount_paid="125.50000000",
            payment_outputs=["ab" * 32 + ":0"],
            terms_version="2026-09-18",
            entitlements=["dmd_did_holder"],
            issued_at=NOW,
        ),
        issuer,
    )
    assert credential["type"] == ["VerifiableCredential", "DebitMyDataRegistrationCredential"]
    assert credential["issuer"]["id"] == issuer.did
    subject = credential["credentialSubject"]
    assert subject["id"] == did
    assert subject["registration"]["payment"]["outputs"] == ["ab" * 32 + ":0"]
    assert subject["registration"]["commercialPrice"] == {"currency": "USD", "amount": "25.00"}
    assert subject["entitlements"] == ["dmd_did_holder"]
    assert crypto.verify_proof(credential, issuer.public_key_multibase)


def test_challenge_message_is_stable() -> None:
    message = challenge_message(
        audience="https://api.debitmydata.com",
        registration_intent_id="dmd_did_intent_X",
        controller=AGENT_CONTROLLER,
        nonce="n0nce",
        issued_at=NOW,
        expires_at=NOW.replace(minute=5),
    )
    assert message == (
        "DebitMyData controller proof\n"
        "Audience: https://api.debitmydata.com\n"
        "Purpose: did_registration\n"
        "Registration: dmd_did_intent_X\n"
        f"Controller: {AGENT_CONTROLLER}\n"
        "Nonce: n0nce\n"
        "Issued: 2026-10-19T18:00:00Z\n"
        "Expires: 2026-10-19T18:05:00Z"
    )
    key = crypto.parse_controller_key(AGENT_CONTROLLER)
    signature = crypto.decode_signature(sign_challenge(message))
    assert key.verify(signature, message.encode())


# --------------------------------------------------------------------------- issuer key


def test_issuer_key_file_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "secrets" / "issuer.pem"
    assert main([str(path)]) == 0
    assert "Public key: z6Mk" in capsys.readouterr().out
    assert path.stat().st_mode & 0o777 == 0o600
    key = load_issuer_key(path, "did:web:example.com")
    assert key.verification_method == f"did:web:example.com#{key.public_key_multibase}"
    assert main([str(path)]) == 1  # never overwrites an existing key


def test_issuer_key_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "issuer.pem"
    assert main([str(path)]) == 0
    path.chmod(0o644)
    with pytest.raises(IssuerKeyError, match="chmod 600"):
        load_issuer_key(path, "did:web:example.com")
    with pytest.raises(IssuerKeyError, match="not found"):
        load_issuer_key(tmp_path / "missing.pem", "did:web:example.com")


# --------------------------------------------------------------------------- settings


def test_public_base_url_must_be_an_origin() -> None:
    assert Settings(_env_file=None, public_base_url="http://localhost:8000/").public_base_url == (
        "http://localhost:8000"
    )
    for bad in ("http://localhost:8000/gateway", "localhost:8000", "https://x.com/?a=1"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, public_base_url=bad)


def test_production_refuses_a_signing_key_on_the_api_host(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="ID-15"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+psycopg://prod:secret@db/prod",
            public_base_url="https://api.debitmydata.com",
            issuer_key_path=tmp_path / "issuer.pem",
        )
