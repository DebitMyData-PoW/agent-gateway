"""Agent-side helper for demos and testing: the agent's key, its signatures, and checking
a DebitMyData credential.

An agent normally does this with its own wallet or key manager. This script only exists so
the flow can be run from a terminal:

    python scripts/demo_agent.py new-key agent-key.pem        # prints the public key
    python scripts/demo_agent.py public-key agent-key.pem
    curl ... /controller-challenges | python scripts/demo_agent.py sign agent-key.pem
    python scripts/demo_agent.py verify-credential registration.json
    python scripts/demo_agent.py mcp-tools                      # tools of the MCP server
    python scripts/demo_agent.py mcp-call TOOL '{"arg": "value"}'

``sign`` reads either the challenge JSON returned by the gateway or the bare message.
``verify-credential`` resolves the issuer's did:web DID document and checks the signature.
``mcp-tools`` and ``mcp-call`` talk to the gateway's MCP endpoint like any MCP client would.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dmd_gateway.identity.crypto import ED25519_PUB_MULTICODEC, encode_signature, multibase_encode
from dmd_gateway.identity.crypto import verify_proof as check_proof
from dmd_gateway.identity.issuer import write_new_key

MCP_URL = "http://localhost:8000/mcp"


def _load(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{path} is not an Ed25519 key")
    return key


def _public(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed25519:" + multibase_encode(ED25519_PUB_MULTICODEC + raw)


def new_key(args: argparse.Namespace) -> int:
    try:
        write_new_key(args.path)
    except FileExistsError:
        print(f"{args.path} already exists", file=sys.stderr)
        return 1
    print(_public(_load(args.path)))
    return 0


def public_key(args: argparse.Namespace) -> int:
    print(_public(_load(args.path)))
    return 0


def sign(args: argparse.Namespace) -> int:
    text = sys.stdin.read()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        message = parsed["message"]
    else:
        # The challenge message never ends with a newline; drop the one `echo` adds.
        message = text[:-1] if text.endswith("\n") else text
    print(encode_signature(_load(args.path).sign(message.encode("utf-8"))))
    return 0


def _did_web_url(did: str) -> str:
    parts = did.split(":")
    if parts[:2] != ["did", "web"] or len(parts) < 3:
        raise SystemExit(f"not a did:web identifier: {did}")
    host = unquote(parts[2])
    path = "/".join(parts[3:])
    local = host.split(":")[0] in ("localhost", "127.0.0.1")
    base = f"{'http' if local else 'https'}://{host}"
    return f"{base}/{path}/did.json" if path else f"{base}/.well-known/did.json"


def verify_credential(args: argparse.Namespace) -> int:
    data: Any = json.loads(Path(args.file).read_text() if args.file != "-" else sys.stdin.read())
    credential = data.get("credential", data) if isinstance(data, dict) else data
    issuer = credential["issuer"]["id"]
    method_id = credential["proof"]["verificationMethod"]
    document = httpx.get(_did_web_url(issuer), timeout=10).json()
    methods = {m["id"]: m for m in document.get("verificationMethod", [])}
    if method_id not in methods or method_id not in document.get("assertionMethod", []):
        print(f"INVALID: {method_id} is not an assertion key of {issuer}")
        return 1
    if not check_proof(credential, methods[method_id]["publicKeyMultibase"]):
        print("INVALID: the signature does not match")
        return 1
    subject = credential["credentialSubject"]
    print(f"valid: {subject['id']} ({subject['displayName']}), signed by {issuer}")
    return 0


async def _mcp(url: str, tool: str | None, arguments: dict[str, Any]) -> int:
    from mcp import Client

    async with Client(url) as client:
        if tool is None:
            for item in (await client.list_tools()).tools:
                print(f"{item.name:40} {item.title or ''}")
            return 0
        result = await client.call_tool(tool, arguments)
    if result.is_error:
        print(result.content[0].text, file=sys.stderr)
        return 1
    print(json.dumps(result.structured_content, indent=2))
    return 0


def mcp_tools(args: argparse.Namespace) -> int:
    return asyncio.run(_mcp(args.url, None, {}))


def mcp_call(args: argparse.Namespace) -> int:
    return asyncio.run(_mcp(args.url, args.tool, json.loads(args.arguments)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo_agent.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(required=True)
    for name, func, help_text in (
        ("new-key", new_key, "create an agent key file and print its public key"),
        ("public-key", public_key, "print the public key of a key file"),
        ("sign", sign, "sign a controller challenge read from stdin"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("path", type=Path)
        command.set_defaults(func=func)
    verify = commands.add_parser("verify-credential", help="check a credential's signature")
    verify.add_argument("file", help="JSON file with the credential (or - for stdin)")
    verify.set_defaults(func=verify_credential)
    tools = commands.add_parser("mcp-tools", help="list the tools of the MCP server")
    tools.add_argument("--url", default=MCP_URL)
    tools.set_defaults(func=mcp_tools)
    call = commands.add_parser("mcp-call", help="call an MCP tool and print its result")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON object of arguments")
    call.add_argument("--url", default=MCP_URL)
    call.set_defaults(func=mcp_call)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
