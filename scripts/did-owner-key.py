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

    python3 did-owner-key.py token  KEYS  did:web:...  [--show] [--identity]
        Signs in as the DID-LLM at DebitMyData's authorization server (OAuth): signs a short
        request with the owner key and gets an access token for the Agentic Superhighway (MCP),
        valid for a few minutes. --show prints the token itself: keep it private, whoever holds
        it can act as the DID-LLM until it expires. --identity: a token for the Agentic
        Superhighway's identity endpoint (.../agenticsuperhighway/identity) instead.

    python3 did-owner-key.py whoami  KEYS  did:web:...  [--identity]
        Signs in as above, then asks the Agentic Superhighway (or with --identity, its identity
        endpoint) who it is signed in as: checks the whole sign-in from start to end. The token
        is never printed.

    python3 did-owner-key.py sign-in  KEYS  https://auth.debitmydata.io/authorize/dmd_signin_...
        Signs you in to an app (ChatGPT, Claude...) with the DID-LLM, from DebitMyData's sign-in
        page (the address it shows): shows which app asks and what for, asks you to confirm,
        signs with the owner key and sends the signature. Then click Continue on the page.

    python3 did-owner-key.py change-owner  KEYS  did:web:...  NEW-OWNER  [NEW-OWNER]
        Moves the DID-LLM to new owners: NEW-OWNER is a new key file (made with "new"), a public
        key (ed25519:z6Mk...) or a wallet (0x... on Base, or eip155:<chain id>:0x...); name every
        owner it will have afterwards (to keep the current key, name it too). Shows the change,
        asks you to confirm, then signs what your keys can sign: the current owner key approves,
        a new owner key accepts. A wallet's message is shown for you to sign in the wallet and
        paste its signature. Earlier sign-ins end once the change applies.

    python3 did-owner-key.py sign-change  KEYS  did:web:...  dmd_owner_change_...
        Signs an owner change made on the "Manage your DID-LLM" page: shows the owners now and
        the new ones, asks you to confirm, then signs what your keys can sign (the current owner
        key approves, a new owner key accepts) and sends the signatures. Wallets sign on the
        page. Then click Continue there.

    python3 did-owner-key.py revoke  KEYS  did:web:...
        Revokes the DID-LLM for good, with the owner key: it cannot be undone. Shows what you are
        signing and asks you to type REVOKE first.

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
OWNER_CHANGE = "DebitMyData owner change"
REVOCATION = "DebitMyData revocation"
WALLET = re.compile(r"^(0x[0-9a-fA-F]{40}|eip155:[0-9]{1,12}:0x[0-9a-fA-F]{40})$")
# Only https (or a gateway running on this machine, for testing).
GATEWAY = re.compile(r"^(https://[^/\s]+|http://(localhost|127\.0\.0\.1)(:\d+)?)$")
AGENT_DID = re.compile(r"^did:web:([A-Za-z0-9.%-]+):agents:([A-Za-z0-9_-]+)$")
CHANGE_ID = re.compile(r"^dmd_owner_change_[0-9A-HJKMNP-TV-Z]{26}$")
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


SIGN_IN_PAGE = re.compile(
    r"^((?:https://[^/\s]+)|(?:http://(?:localhost|127\.0\.0\.1)(?::\d+)?))"
    r"/authorize/(dmd_signin_[0-9A-HJKMNP-TV-Z]{26})$"
)


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


def fetch_json(url: str, body: dict | None = None) -> tuple[int, dict]:
    """GET ``url``, or POST ``body`` to it as JSON; the answer's status and JSON."""
    if not GATEWAY.match(re.sub(r"^(https?://[^/]+).*$", r"\1", url)):
        sys.exit(f"The sign-in server must be an https:// address, not {url!r}")
    request = urllib.request.Request(  # noqa: S310 (https only, checked above)
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={
            "Content-Type": "application/json",
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
            return error.code, {"error": {"code": str(error.code), "message": error.reason}}
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


def find_key(path: str, controller: str) -> Ed25519PrivateKey | None:
    """The key in ``path`` (a key file, or a folder of key files) whose public key is
    ``controller``, or None."""
    files = (
        [path]
        if not os.path.isdir(path)
        else [os.path.join(path, name) for name in sorted(os.listdir(path))]
    )
    for file in files:
        try:
            with open(file, "rb") as handle:
                key = serialization.load_pem_private_key(handle.read(), password=None)
        except (OSError, ValueError, TypeError):
            continue  # not a key file
        if isinstance(key, Ed25519PrivateKey) and public_key(key) == controller:
            print(f"The key {controller[:20]}... is {file}.")
            return key
    return None


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


def sign_in(path: str, did: str, identity_endpoint: bool = False) -> tuple[str, dict]:
    """Signs in as the DID-LLM: the MCP server's address (``identity_endpoint``: its identity
    endpoint's) and the authorization server's answer (its access_token, expires_in and
    scope)."""
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
            "This DID-LLM is controlled by a wallet: it signs in with the wallet (a message "
            "from the sign-in server's /challenge), not with this command. Nothing was signed."
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
    if identity_endpoint:
        resource = str(resource).rstrip("/") + "/identity"
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


def token(path: str, did: str, show: bool = False, identity_endpoint: bool = False) -> None:
    resource, answer = sign_in(path, did, identity_endpoint)
    minutes = max(1, int(answer.get("expires_in", 0)) // 60)
    print(f"Signed in as {did}.")
    print(f"Access token for {resource} (scope: {answer.get('scope')}), valid {minutes} minutes.")
    if show:
        print("Keep it private: whoever holds it can act as this DID-LLM until it expires.")
        print(answer["access_token"])
    else:
        print("Add --show to print the token itself.")


def whoami(path: str, did: str, identity_endpoint: bool = False) -> None:
    """Signs in, then asks the MCP server who it is signed in as: the whole sign-in, checked."""
    resource, answer = sign_in(path, did, identity_endpoint)
    print(f"Signed in as {did}; asking {resource} who that is...")
    status, reply = mcp_call(resource, answer["access_token"], "debitmydata_whoami")
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
    where = "Its identity endpoint" if identity_endpoint else "The Agentic Superhighway"
    print(f"{where} says: signed in as {did}")
    print(f"  status: {identity.get('status')}, name: {identity.get('display_name')}")
    print(f"  scope: {content.get('scope')}, token valid until {content.get('token_expires_at')}")


def approve(path: str, page: str, confirm=input) -> None:
    """Sign in to an app with the DID-LLM, from DebitMyData's sign-in page."""
    page = page.strip().rstrip("/")
    found = SIGN_IN_PAGE.match(page)
    if not found:
        sys.exit(
            "Give the sign-in page's address, as the page shows it: "
            "https://auth.debitmydata.io/authorize/dmd_signin_..."
        )
    if not os.path.exists(path):
        sys.exit(f"No key file or folder at {path}.")
    server = found.group(1)
    status, request = fetch_json(f"{page}/request")
    if status != 200:
        sys.exit(f"No such sign-in (HTTP {status}): {problem(request)}")
    if request.get("status") != "open":
        sys.exit(f"This sign-in is {request.get('status')}: start again from the app.")
    controller = request.get("controller") or {}
    message = request.get("message")
    if not message or not controller:
        sys.exit("Enter your DID-LLM on the page first (and Continue), then run this again.")
    if controller.get("type") != "key":
        sys.exit("This sign-in is for the DID-LLM's wallet: sign on the page, with the wallet.")
    key = owner_key(path, str(controller.get("value")), "DID-LLM")
    did = str(request.get("did") or "")
    lines = message.split("\n")
    if (
        lines[0] != "DebitMyData sign-in"
        or f"DID-LLM: {did}" not in lines
        or f"Sign-in server: {server}" not in lines
    ):
        sys.exit(f"This is not a DebitMyData sign-in at {server}: nothing was signed.")
    print("You are about to sign in:")
    print("  " + "\n  ".join(lines[1:4]))
    if confirm("Sign in? Type yes: ").strip().lower() not in ("y", "yes"):
        sys.exit("Not signed.")
    signature = b64url(key.sign(message.encode("utf-8")))
    status, answer = fetch_json(f"{page}/signature", {"signature": signature})
    if status != 200:
        sys.exit(f"The signature was refused (HTTP {status}): {problem(answer)}")
    print("Signed in. Back on the page, click Continue.")


def did_path(did: str) -> str:
    if not AGENT_DID.match(did):
        sys.exit("Give the DID-LLM itself: did:web:...:agents:...")
    return urllib.parse.quote(did, safe=":")


def new_owners(values: list[str]) -> dict:
    """NEW-OWNER arguments -> the owner change request: a key file, a public key or a wallet."""
    body: dict = {}
    for value in values:
        if value.startswith("ed25519:"):
            field, owner = "new_controller_public_key", value
        elif WALLET.match(value):
            field, owner = "new_controller_wallet_address", value
        elif os.path.isfile(value):
            field, owner = "new_controller_public_key", public_key(load(value))
        else:
            sys.exit(
                f"{value!r} is not a key file, a public key (ed25519:z6Mk...) or a wallet (0x...)."
            )
        if field in body:
            sys.exit("Name at most one owner key and one owner wallet.")
        body[field] = owner
    return body


def _checked(message: str, did: str, change_id: str, signer: str) -> list[str]:
    lines = message.split("\n")
    if (
        lines[:3] != [OWNER_CHANGE, f"Audience: {API}", "Purpose: rotation"]
        or f"DID-LLM: {did}" not in lines
        or f"Change: {change_id}" not in lines
        or f"Signer: {signer}" not in lines
    ):
        sys.exit(f"This is not an owner change of {did} at {API}: nothing was signed.")
    return lines


def _show_change(change: dict, did: str, confirm) -> None:
    def owners(items: list) -> str:
        return ", ".join(f"{item['type']} {item['value']}" for item in items) or "-"

    print(f"Owner change {change['change_id']} of {did}:")
    print("  Owners now:  " + owners(change["previous_owners"]))
    print("  New owners:  " + owners(change["new_owners"]))
    print("Only the new owners will control it, and every sign-in made before ends.")
    if confirm("Change the owners? Type yes: ").strip().lower() not in ("y", "yes"):
        sys.exit("Nothing was signed.")


def _sign_change(path: str, did: str, change: dict, paste=None) -> dict:
    """Signs the change's open messages that the keys in ``path`` can sign; a wallet's message
    is shown for its signature to be pasted (``paste``), or left to the page (None). Returns
    the change as it stands after."""
    where = did_path(did)
    view = change
    for item in change["messages"]:
        if view.get("status") != "open":
            break
        if item.get("status", "open") != "open":
            continue  # signed already (or used up)
        if item["role"] == "approve" and not view["waiting_for"]["approval"]:
            continue  # one approval is enough
        lines = _checked(item["message"], did, change["change_id"], item["signer"])
        if item["signer"].startswith("ed25519:"):
            key = find_key(path, item["signer"])
            if key is None:
                print(f"No key file in {path} for {item['signer']}: its message is not signed.")
                continue
            signature = base64.urlsafe_b64encode(key.sign(item["message"].encode("utf-8")))
            answer = signature.rstrip(b"=").decode("ascii")
        elif paste is None:
            continue  # a wallet signs on the page
        else:
            print(f"\nSign this message with the wallet {item['signer']} ({item['role']}):")
            print("  " + "\n  ".join(lines))
            answer = paste("Paste the wallet's signature (0x...), or leave empty to skip: ")
            answer = answer.strip()
            if not answer:
                continue
        status, view = call(
            "POST",
            f"/v1/dids/{where}/owner-changes/{change['change_id']}/signatures",
            {"challenge_id": item["challenge_id"], "signature": answer},
        )
        if status != 201:
            sys.exit(f"The signature was refused (HTTP {status}): {problem(view)}")
        print(f"Signed ({item['role']}) by {item['signer']}.")
    return view


def _still_needed(view: dict) -> str:
    waiting = view.get("waiting_for", {})
    missing = (["a current owner's approval"] if waiting.get("approval") else []) + [
        f"the acceptance of {owner}" for owner in waiting.get("acceptances", [])
    ]
    return ", ".join(missing)


def change_owner(path: str, did: str, values: list[str], confirm=input, paste=input) -> None:
    where = did_path(did)
    if not os.path.exists(path):
        sys.exit(f"No key file or folder at {path}.")
    status, change = call("POST", f"/v1/dids/{where}/owner-changes", new_owners(values))
    if status != 201:
        sys.exit(f"The change was refused (HTTP {status}): {problem(change)}{elsewhere(status)}")
    _show_change(change, did, confirm)
    view = _sign_change(path, did, change, paste)
    if view.get("status") == "applied":
        print("Done: the DID-LLM has its new owners. Earlier sign-ins have ended.")
        return
    sys.exit(
        "Not applied yet: still needed: " + _still_needed(view) + f". The change "
        f"{change['change_id']} waits until {change['expires_at']} (the MCP tool "
        "debitmydata_change_owner takes the other signatures)."
    )


def sign_change(path: str, did: str, change_id: str, confirm=input) -> None:
    """Signs an owner change made elsewhere (the Manage your DID-LLM page): the messages of the
    keys in ``path``; wallets sign on the page."""
    where = did_path(did)
    if not CHANGE_ID.match(change_id):
        sys.exit("Give the owner change's ID: dmd_owner_change_... (the page shows it).")
    if not os.path.exists(path):
        sys.exit(f"No key file or folder at {path}.")
    status, change = call("GET", f"/v1/dids/{where}/owner-changes/{change_id}")
    if status != 200:
        sys.exit(f"No such owner change (HTTP {status}): {problem(change)}{elsewhere(status)}")
    if change.get("did") != did or change.get("change_id") != change_id:
        sys.exit(f"This is not an owner change of {did}: nothing was signed.")
    if change.get("status") != "open":
        sys.exit(f"This owner change is {change.get('status')}: nothing to sign.")
    _show_change(change, did, confirm)
    view = _sign_change(path, did, change)
    if view.get("status") == "applied":
        print("Done: the DID-LLM has its new owners. Earlier sign-ins have ended.")
    elif _still_needed(view):
        print("Signed what your keys can sign. Still needed: " + _still_needed(view) + ".")
    print("Back on the page, click Continue.")


def revoke(path: str, did: str, confirm=input) -> None:
    where = did_path(did)
    if not os.path.exists(path):
        sys.exit(f"No key file or folder at {path}.")
    status, challenge = call(
        "POST", f"/v1/dids/{where}/revocations", {"controller_type": "ed25519"}
    )
    if status != 201:
        sys.exit(f"No message to sign (HTTP {status}): {problem(challenge)}{elsewhere(status)}")
    key = owner_key(path, str(challenge.get("controller")), "DID-LLM")
    message = challenge["message"]
    lines = message.split("\n")
    if (
        lines[:3] != [REVOCATION, f"Audience: {API}", "Purpose: revocation"]
        or f"DID-LLM: {did}" not in lines
        or f"Controller: {public_key(key)}" not in lines
    ):
        sys.exit(f"This is not a revocation of {did} at {API}: nothing was signed.")
    print("You are about to sign:")
    print("  " + "\n  ".join(lines[3:-3]))
    print("This revokes the DID-LLM for good. It cannot be undone.")
    if confirm("Type REVOKE to revoke it: ").strip() != "REVOKE":
        sys.exit("Not signed: the DID-LLM is not revoked.")
    signature = base64.urlsafe_b64encode(key.sign(message.encode("utf-8"))).rstrip(b"=")
    status, answer = call(
        "POST",
        f"/v1/dids/{where}/revocations/{challenge['challenge_id']}/signature",
        {"signature": signature.decode("ascii")},
    )
    if status != 201:
        sys.exit(f"The signature was refused (HTTP {status}): {problem(answer)}")
    print("Revoked: the DID-LLM, its credential and its entitlements no longer work.")


OPTIONAL_ID = ([], ["--identity"])

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "new":
        new(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "prove":
        prove(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "directory":
        directory(sys.argv[2], sys.argv[3])
    elif (
        len(sys.argv) in (4, 5, 6)
        and sys.argv[1] == "token"
        and set(sys.argv[4:]) <= {"--show", "--identity"}
        and len(set(sys.argv[4:])) == len(sys.argv[4:])
    ):
        token(
            sys.argv[2],
            sys.argv[3],
            show="--show" in sys.argv,
            identity_endpoint="--identity" in sys.argv,
        )
    elif sys.argv[1:2] == ["whoami"] and len(sys.argv) in (4, 5) and sys.argv[4:] in OPTIONAL_ID:
        whoami(sys.argv[2], sys.argv[3], identity_endpoint=sys.argv[4:] == ["--identity"])
    elif len(sys.argv) == 4 and sys.argv[1] == "sign-in":
        approve(sys.argv[2], sys.argv[3])
    elif len(sys.argv) in (5, 6) and sys.argv[1] == "change-owner":
        change_owner(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif len(sys.argv) == 5 and sys.argv[1] == "sign-change":
        sign_change(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) == 4 and sys.argv[1] == "revoke":
        revoke(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
