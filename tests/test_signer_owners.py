"""The signer checks a DID-LLM's owners against its signed records before it signs a token or
a sign-in grant (signer/owners.py), and keeps the issuer's retired keys (identity/issuer.py).

The signer runs here on a real Unix socket against the test database, as in production. A
"compromised API" is played by writing to the database directly: whatever it writes there,
the signer signs in only the owners the DID-LLM's credential and its owners' signed changes
prove.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway import admin
from dmd_gateway.db.models import (
    AgentIdentity,
    ControllerChallenge,
    ControllerProof,
    OwnerChange,
    RegistrationIntent,
)
from dmd_gateway.db.session import session_factory
from dmd_gateway.domain.ids import CHALLENGE, OWNER_CHANGE, PROOF, new_id
from dmd_gateway.identity.crypto import verify_proof
from dmd_gateway.identity.did import issuer_did_document
from dmd_gateway.identity.issuer import (
    IssuerKey,
    IssuerKeyError,
    IssuerKeys,
    issuer_keys,
    load_issuer_key,
    parse_retired_keys,
    write_new_key,
)
from dmd_gateway.services import owner_changes
from dmd_gateway.signer import policy
from dmd_gateway.signer.client import SignerRefused, call_signer
from dmd_gateway.signer.owners import proven_owners
from dmd_gateway.signer.service import build
from dmd_gateway.signer.tokens import TokenIssuer, TokenRefused, resources_for

from . import test_oauth_browser as browser
from .conftest import TEST_DATABASE_URL
from .fakes import AGENT_CONTROLLER, AGENT_KEY, sign_challenge
from .test_identity_status import issue
from .test_oauth import ISS, MCP, live, token_request
from .test_oauth_browser import ISSUER, gateway  # noqa: F401
from .test_owner_changes import HOST, item, new_key, send, sign, start
from .test_quotes import _app_client, ready_registry  # noqa: F401
from .test_registration import clean_chain, engine, prove, quote, settle  # noqa: F401
from .test_sign_in_grants import (  # noqa: F401
    TOKEN_KEY,
    ask_token,
    exchange,
    production,
    stored_grants,
)
from .test_signer import DID, KEY
from .test_signer_tokens import ask
from .test_wallet_registration import wallet_signature

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
NOT_PROVEN = "not one the DID-LLM's signed records prove"
NOT_NAMED = "not the ones this DID-LLM's signed records name"


def _public(key: Ed25519PrivateKey) -> str:
    return IssuerKey(key, DID).public_key_multibase


# --------------------------------------------------------------------------- no database


def test_retired_issuer_keys_are_read_and_checked() -> None:
    old, older = _public(Ed25519PrivateKey.generate()), _public(Ed25519PrivateKey.generate())
    assert parse_retired_keys("") == ()
    assert parse_retired_keys(f" '{old}, {older},{old}' ") == (old, older)
    for bad in ("z6Mkabc", "ed25519:" + old, old.replace("z6Mk", "z6Ls", 1)):
        with pytest.raises(ValueError, match="not an Ed25519 public key"):
            parse_retired_keys(bad)
    with pytest.raises(ValueError, match="cannot also be a retired one"):
        IssuerKeys(DID, old, (old,))


def test_the_did_document_lists_the_retired_keys_after_the_current_one() -> None:
    current, old = _public(Ed25519PrivateKey.generate()), _public(Ed25519PrivateKey.generate())
    document = issuer_did_document(DID, current, (old,))
    ids = [f"{DID}#{current}", f"{DID}#{old}"]
    assert [m["id"] for m in document["verificationMethod"]] == ids
    assert [m["publicKeyMultibase"] for m in document["verificationMethod"]] == [current, old]
    assert document["assertionMethod"] == ids
    # Without retired keys, as before.
    assert issuer_did_document(DID, current)["assertionMethod"] == [f"{DID}#{current}"]


def test_what_a_retired_key_signed_keeps_verifying() -> None:
    old_key = IssuerKey(Ed25519PrivateKey.generate(), DID)
    new_key_ = IssuerKey(Ed25519PrivateKey.generate(), DID, (old_key.public_key_multibase,))
    now = datetime(2026, 11, 16, tzinfo=UTC)
    document = {"@context": ["https://www.w3.org/ns/credentials/v2"], "id": "urn:x"}
    by_old, by_new = old_key.sign(document, created=now), new_key_.sign(document, created=now)
    keys = issuer_keys(new_key_)
    assert keys.verifies(by_old) and keys.verifies(by_new)
    assert not issuer_keys(IssuerKey(new_key_.private_key, DID)).verifies(by_old)  # not listed
    # A signature by an unknown key, or a changed document, never counts.
    stranger = IssuerKey(Ed25519PrivateKey.generate(), DID).sign(document, created=now)
    assert not keys.verifies(stranger)
    assert not keys.verifies({**by_old, "id": "urn:y"})


def test_a_retired_key_never_signs_again(tmp_path: Path) -> None:
    path = tmp_path / "issuer.pem"
    public = write_new_key(path)
    assert load_issuer_key(path, DID).retired_keys == ()
    with pytest.raises(IssuerKeyError, match="retired issuer key"):
        load_issuer_key(path, DID, (public,))
    # The signer reads them from its settings, and refuses what is not a key.
    environ = {
        "DMD_SIGNER_KEY_PATH": str(path),
        "DMD_SIGNER_SOCKET": str(tmp_path / "s.sock"),
        "DMD_SIGNER_DATABASE_URL": "postgresql+psycopg://dmd_signer@/agent_gateway",
        "DMD_SIGNER_ISSUER_DID": DID,
    }
    old = _public(Ed25519PrivateKey.generate())
    signer, _, engine_ = build({**environ, "DMD_SIGNER_ISSUER_RETIRED_KEYS": old})
    engine_.dispose()
    assert signer.key.retired_keys == (old,)
    assert signer.handle({"op": "describe"})["retired_keys"] == [old]
    with pytest.raises(RuntimeError, match="DMD_SIGNER_ISSUER_RETIRED_KEYS"):
        build({**environ, "DMD_SIGNER_ISSUER_RETIRED_KEYS": "z6Mknope"})


def test_settings_for_retired_issuer_keys() -> None:
    from .test_oauth import _settings

    old = _public(Ed25519PrivateKey.generate())
    settings = _settings(issuer_retired_keys=f"{old}")
    assert settings.retired_issuer_keys == (old,)
    with pytest.raises(ValidationError, match="not an Ed25519 public key"):
        _settings(issuer_retired_keys="z6Mknope")
    with pytest.raises(ValidationError, match="never a retired one"):
        _settings(
            issuer_signer_socket="/run/dmd-signer/signer.sock",
            issuer_public_key=old,
            issuer_retired_keys=old,
        )


# --------------------------------------------------------------------------- with the database


def _sql(engine: Engine, statement: str, **params: Any) -> None:  # noqa: F811
    with engine.begin() as connection:
        connection.execute(text(statement), params)


def _change_owner(client: TestClient, did: str) -> tuple[Ed25519PrivateKey, str]:
    """The DID-LLM's owner key moves to a new key, both signing, through the API."""
    key2, owner2 = new_key()
    change = start(client, did, new_controller_public_key=owner2)
    approve, accept = item(change, "approve", AGENT_CONTROLLER), item(change, "accept", owner2)
    assert send(client, did, change, approve, sign_challenge(approve["message"])).status_code == 201
    done = send(client, did, change, accept, sign(key2, accept["message"]))
    assert done.json()["status"] == "applied", done.text
    return key2, owner2


@needs_db
def test_owners_who_signed_their_change_sign_in_through_the_signer(
    production: tuple[TestClient, Path],  # noqa: F811
) -> None:
    client, socket = production
    did = issue(client)["did"]
    assert ask(socket, live(did))["scope"] == "did:read"
    key2, _ = _change_owner(client, did)
    assert ask(socket, live(did, key=key2))["scope"] == "did:read"
    with pytest.raises(SignerRefused, match="signature"):
        ask(socket, live(did))  # the earlier key: no longer the owner
    # Then a third key, approved by the second: the chain goes on.
    key3, owner3 = new_key()
    change = start(client, did, new_controller_public_key=owner3)
    approve, accept = (
        item(change, "approve", change["previous_owners"][0]["value"]),
        item(change, "accept", owner3),
    )
    send(client, did, change, approve, sign(key2, approve["message"]))
    assert send(client, did, change, accept, sign(key3, accept["message"])).json()["status"] == (
        "applied"
    )
    assert ask(socket, live(did, key=key3))["expires_in"] == 600


@needs_db
def test_an_owner_written_into_the_database_signs_in_to_nothing(
    production: tuple[TestClient, Path],  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    client, socket = production
    did = issue(client)["did"]
    attacker, owner = new_key()
    _sql(
        engine,
        "UPDATE identities SET controller_public_key = :k WHERE public_identifier = :d",
        k=owner,
        d=did,
    )
    with pytest.raises(SignerRefused, match=NOT_NAMED):
        ask(socket, live(did, key=attacker))
    # The API asks the signer, which refuses: no token, whatever the API itself accepted.
    response = token_request(client, live(did, key=attacker))
    assert response.status_code != 200 and "access_token" not in response.text


def _forge_change(
    engine: Engine,  # noqa: F811
    did: str,
    new: tuple[Ed25519PrivateKey, str],
    approval: str | None,
    previous: tuple[Ed25519PrivateKey, str] = (AGENT_KEY, AGENT_CONTROLLER),
) -> None:
    """What a compromised API could write: an applied owner change from ``previous`` to its
    own key ``new``, which it accepts with a real signature (it holds that key), an "approval"
    (``approval``: none, ``attacker`` its own signature, ``replay`` a real signature of the
    owner's on another message, ``owner`` the previous owner's own signature of this change),
    and the new owner in the record."""
    key, owner = new
    previous_key, previous_owner = previous
    with Session(engine) as session, session.begin():
        identity_id = session.execute(
            text("SELECT id FROM identities WHERE public_identifier = :d"), {"d": did}
        ).scalar_one()
        now = datetime.now(UTC).replace(microsecond=0)
        latest = session.execute(
            text("SELECT max(applied_at) FROM owner_changes WHERE identity_id = :i"),
            {"i": identity_id},
        ).scalar()
        if latest is not None and latest >= now:
            now = latest + timedelta(seconds=1)  # after the earlier change, as it would be
        expires = now + timedelta(minutes=15)
        change = OwnerChange(
            id=new_id(OWNER_CHANGE),
            identity_id=identity_id,
            previous_public_key=previous_owner,
            new_public_key=owner,
            status="applied",
            expires_at=expires,
            created_at=now,
            applied_at=now,
        )
        session.add(change)
        session.flush()

        def message(signer: str, role: str, nonce: str) -> str:
            return owner_changes.change_message(
                audience="http://localhost:8000",
                did=did,
                change_id=change.id,
                signer=signer,
                signer_role=role,
                owners_now={"ed25519": previous_owner},
                new_owners={"ed25519": owner},
                nonce=nonce,
                issued_at=now,
                expires_at=expires,
            )

        signed: list[tuple[str, str, str]] = []  # signer, nonce, message, signature
        nonce = new_id(CHALLENGE)
        accept = message(owner, "accept", nonce)
        signed.append((owner, nonce, accept, sign(key, accept)))
        nonce = new_id(CHALLENGE)
        approve = message(previous_owner, "approve", nonce)
        if approval == "attacker":
            signed.append((previous_owner, nonce, approve, sign(key, approve)))
        elif approval == "replay":
            other = "DebitMyData sign-in\nsomething the owner signed once"
            signed.append((previous_owner, nonce, approve, sign(previous_key, other)))
        elif approval == "owner":
            signed.append((previous_owner, nonce, approve, sign(previous_key, approve)))
        for signer, nonce, text_, signature in signed:
            challenge = ControllerChallenge(
                id=new_id(CHALLENGE),
                identity_id=identity_id,
                owner_change_id=change.id,
                purpose=owner_changes.CHANGE_PURPOSE,
                controller_type="ed25519",
                controller_value=signer,
                nonce=nonce,
                message=text_,
                expires_at=expires,
                created_at=now,
                used_at=now,
            )
            session.add(challenge)
            session.flush()
            session.add(
                ControllerProof(
                    id=new_id(PROOF),
                    challenge_id=challenge.id,
                    method="ed25519_signature",
                    signature=signature,
                    status="verified",
                    verified_at=now,
                    created_at=now,
                )
            )
        session.execute(
            text(
                "UPDATE identities SET controller_public_key = :k, controller_changed_at = :t "
                "WHERE id = :i"
            ),
            {"k": owner, "t": now, "i": identity_id},
        )


@needs_db
def test_a_forged_owner_change_proves_nothing(
    production: tuple[TestClient, Path],  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    """The attacker's key accepts for real; only the owner's approval is missing."""
    client, socket = production
    for approval in (None, "attacker", "replay"):
        did = issue(client)["did"]
        new = new_key()
        _forge_change(engine, did, new, approval)
        with pytest.raises(SignerRefused, match=NOT_PROVEN):
            ask(socket, live(did, key=new[0]))
    # The same records with the owner's own approval: the change holds (so what was missing
    # above was the approval, and only that).
    did = issue(client)["did"]
    new = new_key()
    _forge_change(engine, did, new, "owner")
    assert ask(socket, live(did, key=new[0]))["scope"] == "did:read"

    # Two forged changes: the second "approved" by the key the first one forged in. That key
    # signs for real (the attacker holds it), but it never became a proven owner.
    did = issue(client)["did"]
    first, second = new_key(), new_key()
    _forge_change(engine, did, first, None)
    _forge_change(engine, did, second, "owner", previous=first)
    with pytest.raises(SignerRefused, match=NOT_PROVEN):
        ask(socket, live(did, key=second[0]))


@needs_db
def test_a_changed_credential_proves_nothing(
    production: tuple[TestClient, Path],  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    client, socket = production
    issued = issue(client)
    did = issued["did"]
    attacker, owner = new_key()
    # The credential rewritten to name the attacker's key, and the record too.
    _sql(
        engine,
        "UPDATE credentials SET payload = jsonb_set(payload, "
        "'{credentialSubject,controller,publicKeyMultibase}', to_jsonb(CAST(:k AS text))) "
        "WHERE id = :c",
        k=owner.removeprefix("ed25519:"),
        c=issued["credential_id"],
    )
    _sql(
        engine,
        "UPDATE identities SET controller_public_key = :k WHERE public_identifier = :d",
        k=owner,
        d=did,
    )
    with pytest.raises(SignerRefused, match="not one DebitMyData signed for it"):
        ask(socket, live(did, key=attacker))


@needs_db
def test_what_its_owner_revoked_stays_revoked(
    production: tuple[TestClient, Path],  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    client, socket = production
    did = issue(client)["did"]
    asked = client.post(f"/v1/dids/{did}/revocations", json={"reason": "Key leaked"}, headers=HOST)
    message = asked.json()
    done = client.post(
        f"/v1/dids/{did}/revocations/{message['challenge_id']}/signature",
        json={"signature": sign(AGENT_KEY, message["message"])},
        headers=HOST,
    )
    assert done.json()["status"] == "revoked", done.text
    _sql(engine, "UPDATE identities SET status = 'active' WHERE public_identifier = :d", d=did)
    with pytest.raises(SignerRefused, match="revoked by its owner"):
        ask(socket, live(did))


@needs_db
def test_an_app_sign_in_from_before_a_signed_change_stays_ended(
    production: tuple[TestClient, Path],  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    client, socket = production
    did = issue(client)["did"]
    exchange(client, did)
    [(grant, _)] = stored_grants(engine)
    assert ask_token(socket, grant)["scope"] == "did:read"
    _change_owner(client, did)
    # Even if the database forgets when the owners changed, their signed change says it.
    _sql(
        engine,
        "UPDATE identities SET controller_changed_at = NULL WHERE public_identifier = :d",
        d=did,
    )
    with pytest.raises(SignerRefused, match="owners changed after this sign-in"):
        ask_token(socket, grant)


@needs_db
def test_credentials_signed_before_a_key_rotation_still_prove_their_owners(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    with _app_client(ready_registry, engine, issuer_key=KEY) as client:
        did = issue(client)["did"]  # signed with KEY, the key before the rotation

    rotated = IssuerKey(Ed25519PrivateKey.generate(), DID, (KEY.public_key_multibase,))

    def tokens(keys: IssuerKeys) -> TokenIssuer:
        return TokenIssuer(
            key=TOKEN_KEY, issuer=ISS, resources=resources_for(DID), credentials=keys
        )

    request = {"assertion": live(did), "resource": MCP, "scope": "did:read", "ttl_seconds": 600}
    with sessionmaker(engine)() as session:
        answer, facts = tokens(issuer_keys(rotated)).issue(session, request, datetime.now(UTC))
        assert answer["ok"] and facts["did"] == did
        # Had the earlier key not been kept, nothing it signed would count any more.
        forgotten = IssuerKeys(DID, rotated.public_key_multibase)
        with pytest.raises(TokenRefused, match="not one DebitMyData signed for it"):
            tokens(forgotten).issue(session, {**request, "assertion": live(did)}, datetime.now(UTC))

    # The issuer's DID document lists both keys, and the credential still verifies.
    with _app_client(ready_registry, engine, issuer_key=rotated) as client:
        document = client.get("/.well-known/did.json").json()
        assert [m["publicKeyMultibase"] for m in document["verificationMethod"]] == [
            rotated.public_key_multibase,
            KEY.public_key_multibase,
        ]
        checked = client.post("/v1/dids/verifications", json={"did": did}).json()
        assert checked["credential"]["signature_valid"] is True


@needs_db
def test_the_signer_signs_a_credential_for_a_new_did_llm_and_its_own_owners_only(
    ready_registry: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    with _app_client(ready_registry, engine, issuer_key=KEY) as client:
        existing = issue(client)["did"]
        q = quote(client)
        rid = q["registration_intent_id"]
        settle(client, q, txid="b2" * 32)
        prove(client, rid)
    with sessionmaker(engine)() as session:
        registration = session.get(RegistrationIntent, rid)
        assert registration is not None

        def document(did: str, controller: str) -> dict[str, Any]:
            return {
                "@context": ["https://www.w3.org/ns/credentials/v2"],
                "type": ["VerifiableCredential", "DebitMyDataRegistrationCredential"],
                "issuer": {"id": DID, "name": "DebitMyData"},
                "credentialSubject": {
                    "id": did,
                    "displayName": registration.display_name,
                    "participantType": registration.participant_type,
                    "controller": {
                        "type": "Multikey",
                        "publicKeyMultibase": controller.removeprefix("ed25519:"),
                    },
                    "registration": {
                        "registrationIntentId": rid,
                        "payment": {"paymentIntentId": q["payment_intent_id"]},
                    },
                },
            }

        fresh = f"{DID}:agents:01KZZZZZZZZZZZZZZZZZZZZZZZ"
        for did, controller, reason in (
            (existing, AGENT_CONTROLLER, "already exists"),
            ("did:web:evil.example:agents:01KZ", AGENT_CONTROLLER, "not a DID-LLM of this"),
            (fresh, new_key()[1], "other owners than the registration's"),
        ):
            with pytest.raises(policy.Refused, match=reason):
                policy.check(session, document(did, controller), DID)
        facts = policy.check(session, document(fresh, AGENT_CONTROLLER), DID)
        assert facts["registration"] == rid
        signed = KEY.sign(document(fresh, AGENT_CONTROLLER), created=datetime.now(UTC))
        assert verify_proof(signed, KEY.public_key_multibase)


@needs_db
def test_wallets_and_keys_owners_move_between_are_proven_at_each_step(
    gateway: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    did = issue(gateway)["did"]

    def proven() -> dict[str, str]:
        with sessionmaker(engine)() as session:
            identity = session.scalars(
                select(AgentIdentity).where(AgentIdentity.public_identifier == did)
            ).one()
            return proven_owners(session, identity, issuer_keys(ISSUER)).proven

    assert proven() == {"ed25519": AGENT_CONTROLLER}
    wallet = Account.create()
    account = f"eip155:8453:{wallet.address}"
    change = start(gateway, did, new_controller_wallet_address=wallet.address)
    approve, accept = item(change, "approve", AGENT_CONTROLLER), item(change, "accept", account)
    send(gateway, did, change, approve, sign_challenge(approve["message"]))
    send(gateway, did, change, accept, wallet_signature(wallet, accept["message"]))
    assert proven() == {"eip155": account}

    # The wallet approves a key next to it; the key accepts.
    key2, owner2 = new_key()
    both = start(
        gateway, did, new_controller_public_key=owner2, new_controller_wallet_address=wallet.address
    )
    approve, accept = item(both, "approve", account), item(both, "accept", owner2)
    send(gateway, did, both, approve, wallet_signature(wallet, approve["message"]))
    send(gateway, did, both, accept, sign(key2, accept["message"]))
    assert proven() == {"ed25519": owner2, "eip155": account}

    # The key alone: the wallet is gone, the key stays proven.
    alone = start(gateway, did, new_controller_public_key=owner2)
    approve = item(alone, "approve", owner2)
    assert send(gateway, did, alone, approve, sign(key2, approve["message"])).json()["status"] == (
        "applied"
    )
    assert proven() == {"ed25519": owner2}


@needs_db
def test_a_forged_owner_signs_no_person_in_to_an_app(
    production: tuple[TestClient, Path],  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    client, socket = production
    did = issue(client)["did"]
    new = new_key()
    _forge_change(engine, did, new, "attacker")
    _, challenge_value = browser.pkce()
    page = browser.start(client, challenge_value)
    chosen = browser.at_auth(client, "POST", f"{page}/did", json={"did": did}).json()
    request = {
        "message": chosen["message"],
        "signature": sign(new[0], chosen["message"]),
        "did": did,
        "controller_type": "ed25519",
        "client_id": browser.APP,
        "scope": "did:read",
        "resource": MCP,
    }
    with pytest.raises(SignerRefused, match=NOT_PROVEN):
        call_signer(socket, {"op": "sign_in", **request}, 5)


@needs_db
def test_the_operator_checks_every_did_llms_owners(
    gateway: TestClient,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    def check(keys: IssuerKeys | None) -> tuple[int, str]:
        lines: list[str] = []
        args = admin.parser().parse_args(["owners", "check"])
        code = admin.run(args, session_factory(engine), out=lines.append, keys=keys)
        return code, "\n".join(lines)

    keys = issuer_keys(ISSUER)
    # Only the DID-LLMs made here: the earlier tests' are set aside (revoked ones are skipped).
    _sql(engine, "UPDATE identities SET status = 'revoked'")
    good = issue(gateway)["did"]
    _change_owner(gateway, good)
    assert check(keys) == (
        0,
        "Checked 1 DID-LLMs: every owner is proven by its signed records.",
    )
    bad = issue(gateway)["did"]
    _, owner = new_key()
    _sql(
        engine,
        "UPDATE identities SET controller_public_key = :k WHERE public_identifier = :d",
        k=owner,
        d=bad,
    )
    code, text_ = check(keys)
    assert code == 1 and "Checked 2 DID-LLMs: 1 with owners the signer refuses." in text_
    assert bad in text_ and good not in text_ and NOT_NAMED in text_
    assert check(None) == (
        1,
        "DID-LLM issuance is not set up here: no issuer key to check credentials with.",
    )


def test_the_operator_check_reads_the_issuer_keys_from_the_settings(tmp_path: Path) -> None:
    from .test_oauth import _settings

    old = _public(Ed25519PrivateKey.generate())
    settings = _settings(
        public_base_url="https://api.debitmydata.io",
        issuer_signer_socket="/run/dmd-signer/signer.sock",
        issuer_public_key=KEY.public_key_multibase,
        issuer_retired_keys=old,
    )
    assert admin.issuer_public_keys(settings) == IssuerKeys(
        "did:web:api.debitmydata.io", KEY.public_key_multibase, (old,)
    )
    path = tmp_path / "issuer.pem"
    public = write_new_key(path)
    staging = _settings(public_base_url="https://staging-api.debitmydata.io", issuer_key_path=path)
    keys = admin.issuer_public_keys(staging)
    assert keys is not None and keys.current == public and keys.retired == ()
    assert admin.issuer_public_keys(_settings()) is None
