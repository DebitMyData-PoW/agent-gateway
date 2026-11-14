#!/usr/bin/env python3
"""A DID-LLM owner key (Ed25519), kept on your own machine.

    python3 did-owner-key.py new  KEYFILE
        Makes a new key, saves it in KEYFILE (readable by you only) and prints its public key
        (ed25519:z6Mk...), to paste under "My agent's public key" on the Get a DID-LLM page.

    python3 did-owner-key.py prove  KEYS  dmd_did_intent_...
        Once the registration is paid: asks the gateway for the message to sign, signs it with
        the owner key and sends the signature. Then click Continue on the Get a DID-LLM page.

    python3 did-owner-key.py directory  KEYS  dmd_challenge_...
        Signs a directory listing made on the directory's "List your DID-LLM" page (listing the
        DID-LLM, changing its listing or removing it): shows what you are signing, asks you to
        confirm, signs it with the owner key and sends the signature. Then click Continue.

    python3 did-owner-key.py token  KEYS  did:web:...  [--show]
        Signs in as the DID-LLM at DebitMyData's authorization server (OAuth): signs a short
        request with the owner key and gets an access token for the Agentic Superhighway (MCP),
        valid for a few minutes. --show prints the token itself: keep it private, whoever holds
        it can act as the DID-LLM until it expires.

    python3 did-owner-key.py whoami  KEYS  did:web:...
        Signs in as above, then asks the Agentic Superhighway who it is signed in as: checks
        the whole sign-in from start to end. The token is never printed.

KEYS is a key file, or a folder of key files (such as ~/did-keys): the tool then uses the one
that owns the DID-LLM, and says so.

The private key never leaves this machine: only the public key and signatures are sent.
Keep KEYFILE (and a backup of it): it is the proof that the DID-LLM is yours.
Set DMD_GATEWAY_API to use another gateway, e.g. https://staging-api.debitmydata.io
(default: https://api.debitmydata.io); the authorization server is then its auth. address
(https://auth.debitmydata.io, https://staging-auth.debitmydata.io), or DMD_AUTH_SERVER if
set. Needs Python 3.10+ and the "cryptography" package
(Debian/Ubuntu: apt install python3-cryptography). See docs/owner-key.md.
"""

import base64
import json
import os
import re
import sys
import time
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
AGENT_DID = re.compile(r"^did:web:([A-Za-z0-9.%-]+):agents:([A-Za-z0-9_-]+)$")
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def default_auth_server(api: str) -> str:
    """The authorization server next to a gateway: api.X -> auth.X, staging-api.X ->
    staging-auth.X. A gateway on this machine (testing) keeps its own address."""
    scheme, _, host = api.partition("://")
    first, dot, domain = host.partition(".")
    if dot and first.endswith("api"):
        return f"{scheme}://{first[: -len('api')]}auth.{domain}"
    return api


AUTH = os.environ.get("DMD_AUTH_SERVER", "").rstrip("/") or default_auth_server(API)


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


def fetch(url: str, form: dict | None = None) -> tuple[int, dict]:
    """GET ``url``, or POST ``form`` to it as an OAuth form; the answer's status and JSON."""
    if not GATEWAY.match(re.sub(r"^(https?://[^/]+).*$", r"\1", url)):
        sys.exit(f"The authorization server must be an https:// address, not {url!r}")
    request = urllib.request.Request(  # noqa: S310 (https only, checked above)
        url,
        data=urllib.parse.urlencode(form).encode("ascii") if form is not None else None,
        method="POST" if form is not None else "GET",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "dmd-did-owner-key",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, {"error": str(error.code), "error_description": error.reason}
    except urllib.error.URLError as error:
        sys.exit(f"Could not reach {url}: {error.reason}")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def signed_request(key: Ed25519PrivateKey, did: str, audience: str, now: int) -> str:
    """The OAuth client assertion (RFC 7523): a JWT about the DID-LLM, signed with its key,
    valid for one minute and used once (jti)."""
    header = {"alg": "EdDSA", "typ": "JWT"}
    claims = {
        "iss": did,
        "sub": did,
        "aud": audience,
        "iat": now,
        "exp": now + 60,
        "jti": b64url(os.urandom(16)),
    }
    parts = [b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)]
    signing_input = ".".join(parts)
    return f"{signing_input}.{b64url(key.sign(signing_input.encode('ascii')))}"


def elsewhere(status: int) -> str:
    """A 404 from the default gateway often means the page was on another one (staging)."""
    if status != 404 or "DMD_GATEWAY_API" in os.environ:
        return ""
    return (
        f"\nThis asked {API}. If the page is on another server, say which, for example:\n"
        "  DMD_GATEWAY_API=https://staging-api.debitmydata.io python3 did-owner-key.py ..."
    )


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


def owner_key(path: str, controller: str, what: str) -> Ed25519PrivateKey:
    """The owner key in ``path``: that key file, or the key file in that folder whose public key
    is ``controller``. Never another key: anything else stops here, before signing."""
    if not os.path.isdir(path):
        key = load(path)
        mine = public_key(key)
        if mine != controller:
            sys.exit(
                f"This {what} is owned by {controller}, not by this key ({mine}): nothing was "
                "signed.\nGive the folder of your key files instead (for example ~/did-keys): "
                "the tool then finds the one that owns it, if you have it."
            )
        return key
    found = []
    for name in sorted(os.listdir(path)):
        file = os.path.join(path, name)
        try:
            with open(file, "rb") as handle:
                key = serialization.load_pem_private_key(handle.read(), password=None)
        except (OSError, ValueError, TypeError):
            continue  # not a key file
        if not isinstance(key, Ed25519PrivateKey):
            continue
        mine = public_key(key)
        if mine == controller:
            print(f"The owner key is {file}.")
            return key
        found.append(f"  {file}  {mine}")
    sys.exit(
        f"No key file in {path} owns this {what}: its owner key is\n  {controller}\n"
        + ("Your key files there:\n" + "\n".join(found) if found else "No key file there.")
        + f"\nThe {what} was registered with another key, and only that key can sign for it. "
        "(A DID-LLM made on staging's guided test page has a throwaway key that only that page "
        "had: nobody can sign for it afterwards.) Nothing was signed."
    )


def prove(path: str, registration_id: str) -> None:
    if not REGISTRATION.match(registration_id):
        sys.exit("The registration ID starts with dmd_did_intent_ (Copy button at step 1).")
    if not os.path.exists(path):
        sys.exit(f"No key file or folder at {path}.")
    status, challenge = call(
        "POST", f"/v1/dids/registrations/{registration_id}/controller-challenges", {}
    )
    if status not in (200, 201):
        sys.exit(f"No message to sign (HTTP {status}): {problem(challenge)}{elsewhere(status)}")
    key = owner_key(path, str(challenge.get("controller")), "registration")
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
    if not os.path.exists(path):
        sys.exit(f"No key file or folder at {path}.")
    status, challenge = call("GET", f"/v1/directory/challenges/{challenge_id}")
    if status != 200:
        sys.exit(f"No such message (HTTP {status}): {problem(challenge)}{elsewhere(status)}")
    if challenge.get("status") != "open":
        sys.exit(f"This message is {challenge.get('status')}: get a new one on the page.")
    key = owner_key(path, str(challenge.get("controller")), "DID-LLM")
    mine = public_key(key)
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


def sign_in(path: str, did: str) -> tuple[str, dict]:
    """Signs in as the DID-LLM: the MCP server's address and the authorization server's answer
    (its access_token, expires_in and scope)."""
    found = AGENT_DID.match(did)
    if not found:
        sys.exit("A DID-LLM looks like did:web:api.debitmydata.io:agents:... (its page says it).")
    if not os.path.exists(path):
        sys.exit(f"No key file or folder at {path}.")
    host = urllib.parse.unquote(found.group(1))
    if urllib.parse.urlsplit(API).netloc != host:
        sys.exit(
            f"This DID-LLM is from {host}, not {API}. Say so first, for example:\n"
            f"  DMD_GATEWAY_API=https://{host} python3 did-owner-key.py token ..."
        )
    status, document = call("GET", f"/agents/{found.group(2)}/did.json")
    if status != 200:
        sys.exit(f"No such DID-LLM (HTTP {status}): {problem(document)}")
    keys = [
        method.get("publicKeyMultibase")
        for method in document.get("verificationMethod", [])
        if method.get("type") == "Multikey"
    ]
    if not keys:
        sys.exit(
            "This DID-LLM is controlled by a wallet: wallets will sign in through the browser "
            "(not open yet). Nothing was signed."
        )
    key = owner_key(path, f"ed25519:{keys[0]}", "DID-LLM")
    status, server = fetch(f"{AUTH}/.well-known/oauth-authorization-server")
    if status != 200:
        sys.exit(f"The authorization server at {AUTH} is not open (HTTP {status}).")
    endpoint = str(server.get("token_endpoint", ""))
    if server.get("issuer") != AUTH or not endpoint.startswith(f"{AUTH}/"):
        sys.exit(f"{AUTH} does not describe itself as expected: nothing was signed.")
    status, discovery = call("GET", "/v1/discovery")
    resource = discovery.get("mcp_url") if status == 200 else None
    if not resource:
        sys.exit(f"{API} does not give its MCP server's address (HTTP {status}).")
    form = {
        "grant_type": "client_credentials",
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": signed_request(key, did, endpoint, int(time.time())),
        "resource": resource,
        "scope": "did:read",
    }
    status, answer = fetch(endpoint, form)
    if status != 200 or "access_token" not in answer:
        error = f"{answer.get('error', '')}: {answer.get('error_description', '')}".strip(": ")
        sys.exit(f"Refused (HTTP {status}): {error or json.dumps(answer)}")
    return resource, answer


def mcp_call(url: str, access_token: str, tool: str) -> tuple[int, dict]:
    """Calls one MCP tool (JSON-RPC over streamable HTTP) with the access token."""
    if not GATEWAY.match(re.sub(r"^(https?://[^/]+).*$", r"\1", url)):
        sys.exit(f"The MCP server must be an https:// address, not {url!r}")
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }
    request = urllib.request.Request(  # noqa: S310 (https only, checked above)
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "dmd-did-owner-key",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, {"error_description": error.headers.get("WWW-Authenticate", "")}
    except urllib.error.URLError as error:
        sys.exit(f"Could not reach {url}: {error.reason}")


def token(path: str, did: str, show: bool = False) -> None:
    resource, answer = sign_in(path, did)
    minutes = max(1, int(answer.get("expires_in", 0)) // 60)
    print(f"Signed in as {did}.")
    print(f"Access token for {resource} (scope: {answer.get('scope')}), valid {minutes} minutes.")
    if show:
        print("Keep it private: whoever holds it can act as this DID-LLM until it expires.")
        print(answer["access_token"])
    else:
        print("Add --show to print the token itself.")


def whoami(path: str, did: str) -> None:
    """Signs in, then asks the MCP server who it is signed in as: the whole sign-in, checked."""
    resource, answer = sign_in(path, did)
    print(f"Signed in as {did}; asking {resource} who that is...")
    status, reply = mcp_call(resource, answer["access_token"], "dmd_whoami")
    if status != 200:
        reason = reply.get("error_description") or problem(reply)
        sys.exit(f"The MCP server refused the token (HTTP {status}): {reason}")
    result = reply.get("result") or {}
    content = result.get("structuredContent")
    if result.get("isError") or not isinstance(content, dict):
        text = (result.get("content") or [{}])[0].get("text") or json.dumps(reply)
        sys.exit(f"The MCP server answered with an error: {text}")
    identity = content.get("identity") or {}
    if content.get("signed_in_as") != did:
        sys.exit(f"The MCP server says this is {content.get('signed_in_as')}, not {did}.")
    print(f"The Agentic Superhighway says: signed in as {did}")
    print(f"  status: {identity.get('status')}, name: {identity.get('display_name')}")
    print(f"  scope: {content.get('scope')}, token valid until {content.get('token_expires_at')}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "new":
        new(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "prove":
        prove(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "directory":
        directory(sys.argv[2], sys.argv[3])
    elif len(sys.argv) in (4, 5) and sys.argv[1] == "token" and sys.argv[4:] in ([], ["--show"]):
        token(sys.argv[2], sys.argv[3], show=sys.argv[4:] == ["--show"])
    elif len(sys.argv) == 4 and sys.argv[1] == "whoami":
        whoami(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
