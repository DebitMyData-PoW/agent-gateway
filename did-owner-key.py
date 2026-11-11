#!/usr/bin/env python3
"""A DID-LLM owner key (Ed25519), kept on your own machine.

    python3 did-owner-key.py new  KEYFILE
        Makes a new key, saves it in KEYFILE (readable by you only) and prints its public key
        (ed25519:z6Mk...), to paste under "My agent's public key" on the Get a DID-LLM page.

    python3 did-owner-key.py prove  KEYFILE  dmd_did_intent_...
        Once the registration is paid: asks the gateway for the message to sign, signs it with
        KEYFILE and sends the signature. Then reload the Get a DID-LLM page and click Continue.

    python3 did-owner-key.py directory  KEYFILE  dmd_challenge_...
        Signs a directory listing made on the directory's "List your DID-LLM" page (listing the
        DID-LLM, changing its listing or removing it): shows what you are signing, asks you to
        confirm, signs it with KEYFILE and sends the signature. Then click Continue on the page.

The private key never leaves this machine: only the public key and signatures are sent.
Keep KEYFILE (and a backup of it): it is the proof that the DID-LLM is yours.
Set DMD_GATEWAY_API to use another gateway, e.g. https://staging-api.debitmydata.io
(default: https://api.debitmydata.io). Needs Python 3.10+ and the "cryptography" package
(Debian/Ubuntu: apt install python3-cryptography). See docs/owner-key.md.
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

API = os.environ.get("DMD_GATEWAY_API", "https://api.debitmydata.io").rstrip("/")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
REGISTRATION = re.compile(r"^dmd_did_intent_[0-9A-HJKMNP-TV-Z]{26}$")
CHALLENGE = re.compile(r"^dmd_challenge_[0-9A-HJKMNP-TV-Z]{26}$")
LISTING = "DebitMyData directory listing"
# Only https (or a gateway running on this machine, for testing).
GATEWAY = re.compile(r"^(https://[^/\s]+|http://(localhost|127\.0\.0\.1)(:\d+)?)$")


def b58encode(data: bytes) -> str:
    number, text = int.from_bytes(data, "big"), ""
    while number:
        number, rest = divmod(number, 58)
        text = B58[rest] + text
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + text


def public_key(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed25519:z" + b58encode(b"\xed\x01" + raw)  # multicodec ed25519-pub, base58btc


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    if not GATEWAY.match(API):
        sys.exit(f"DMD_GATEWAY_API must be an https:// address, not {API!r}")
    request = urllib.request.Request(  # noqa: S310 (https only, checked above)
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "dmd-did-owner-key"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, {"error": {"message": error.reason}}
    except urllib.error.URLError as error:
        sys.exit(f"Could not reach {API}: {error.reason}")


def problem(answer: dict) -> str:
    error = answer.get("error") if isinstance(answer.get("error"), dict) else answer
    return f"{error.get('code', '')} {error.get('message', '')}".strip() or json.dumps(answer)


def new(path: str) -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    # O_EXCL: never overwrites a key; 0600: readable by you only.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(pem)
    print("Key saved in", path, "(keep it, and a backup of it, private)")
    print("Public key, for the Get a DID-LLM page:")
    print(public_key(key))


def load(path: str) -> Ed25519PrivateKey:
    with open(path, "rb") as file:
        key = serialization.load_pem_private_key(file.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit(f"{path} is not an Ed25519 key")
    return key


def prove(path: str, registration_id: str) -> None:
    if not REGISTRATION.match(registration_id):
        sys.exit("The registration ID starts with dmd_did_intent_ (Copy button at step 1).")
    key = load(path)
    mine = public_key(key)
    status, challenge = call(
        "POST", f"/v1/dids/registrations/{registration_id}/controller-challenges", {}
    )
    if status not in (200, 201):
        sys.exit(f"No message to sign (HTTP {status}): {problem(challenge)}")
    if challenge.get("controller") != mine:
        sys.exit(
            f"This registration is owned by {challenge.get('controller')}, not by this key "
            f"({mine}): nothing was signed."
        )
    message = challenge["message"]
    expected = f"Audience: {API}"
    if expected not in message.split("\n"):
        sys.exit(f"The message is not for {API}: nothing was signed.")
    signature = base64.urlsafe_b64encode(key.sign(message.encode("utf-8"))).rstrip(b"=")
    status, proof = call(
        "POST",
        f"/v1/dids/registrations/{registration_id}/controller-proofs",
        {"challenge_id": challenge["challenge_id"], "signature": signature.decode("ascii")},
    )
    if status not in (200, 201):
        sys.exit(f"The signature was refused (HTTP {status}): {problem(proof)}")
    print("Signed and accepted: the gateway now knows this key owns the DID-LLM.")
    if proof.get("next_action") == "register_did":
        print("Now reload the Get a DID-LLM page and click Continue: it issues the DID-LLM.")
    else:
        print("Next on the page:", proof.get("next_action"), "(pay first, then reload the page).")


def directory(path: str, challenge_id: str, confirm=input) -> None:
    if not CHALLENGE.match(challenge_id):
        sys.exit("The message's ID starts with dmd_challenge_ (step 3 of the listing page).")
    key = load(path)
    mine = public_key(key)
    status, challenge = call("GET", f"/v1/directory/challenges/{challenge_id}")
    if status != 200:
        sys.exit(f"No such message (HTTP {status}): {problem(challenge)}")
    if challenge.get("status") != "open":
        sys.exit(f"This message is {challenge.get('status')}: get a new one on the page.")
    if challenge.get("controller") != mine:
        sys.exit(
            f"This DID-LLM is owned by {challenge.get('controller')}, not by this key "
            f"({mine}): nothing was signed."
        )
    message = challenge["message"]
    lines = message.split("\n")
    did = str(challenge.get("did") or "")
    if (
        lines[:3] != [LISTING, f"Audience: {API}", "Purpose: directory_listing"]
        or f"DID-LLM: {did}" not in lines
        or f"Controller: {mine}" not in lines
    ):
        sys.exit(f"This is not a directory listing for {API} and this key: nothing was signed.")
    print("You are about to sign:")
    print("  " + "\n  ".join(lines[3:-3]))
    if confirm("Sign it? Type yes: ").strip().lower() not in ("y", "yes"):
        sys.exit("Not signed.")
    signature = base64.urlsafe_b64encode(key.sign(message.encode("utf-8"))).rstrip(b"=")
    status, proof = call(
        "POST",
        f"/v1/directory/{urllib.parse.quote(did, safe=':')}/proofs",
        {"challenge_id": challenge_id, "signature": signature.decode("ascii")},
    )
    if status not in (200, 201):
        sys.exit(f"The signature was refused (HTTP {status}): {problem(proof)}")
    if not proof.get("listed"):
        print("Signed and accepted: the DID-LLM is no longer in the directory.")
    elif proof.get("shown"):
        print("Signed and accepted: the DID-LLM is in the directory.")
    else:
        print("Signed and accepted, but the directory does not show it (hidden by DebitMyData).")
    print("Back on the page, click Continue.")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "new":
        new(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "prove":
        prove(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "directory":
        directory(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
