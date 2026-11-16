"""The Agent Skills index: a skill an agent can install to get and use a DID-LLM.

    GET /.well-known/agent-skills/index.json                      the index (Agent Skills
                                                                   discovery, v0.2.0)
    GET /.well-known/agent-skills/debitmydata-did-llm/SKILL.md    the skill

The skill is written by the gateway itself, from the same text as llms.txt (this server's own
addresses, price and open payment options), with a short "when to use it" and the quickest
path through the MCP server at the top. The index gives the skill's SHA-256 digest, computed
from the very text this server serves, so it is always right, at both of the gateway's names.

DebitMyData's shared skill (the debitmydata-skill repository) is linked from it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from dmd_gateway.api.get_did_page import did_price, mcp_url, price_text
from dmd_gateway.api.llms import llms_txt

router = APIRouter(include_in_schema=False)

SCHEMA = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"
SKILL_NAME = "debitmydata-did-llm"  # lowercase letters, digits and hyphens (Agent Skills names)
INDEX_PATH = "/.well-known/agent-skills/index.json"
SKILL_PATH = f"/.well-known/agent-skills/{SKILL_NAME}/SKILL.md"
SHARED_SKILL = "https://github.com/DebitMyData-PoW/debitmydata-skill"
CACHE = "public, max-age=300"  # like llms.txt: it changes only with the configuration


def description(request: Request) -> str:
    """When to use the skill (the index and the SKILL.md frontmatter say the same)."""
    price = price_text(did_price(request))
    return (
        "Get, pay for and use a DebitMyData DID-LLM: a verifiable identity (a W3C did:web DID "
        f"with a credential signed by DebitMyData) for an AI agent, {price}, through "
        "DebitMyData's MCP server or REST API, with no account. Use it when an agent needs an "
        "identity others can verify, wants to sign in to the Agentic Superhighway with it, or "
        "must check another agent's DID-LLM."
    )


def _demoted(text: str) -> str:
    """llms.txt under the skill's own title: each of its headings one level down."""
    return "\n".join(f"#{line}" if line.startswith("#") else line for line in text.splitlines())


def skill_md(request: Request) -> str:
    settings = request.app.state.settings
    base = settings.public_base_url.rstrip("/")
    price = price_text(did_price(request))
    return f"""---
name: {SKILL_NAME}
description: {json.dumps(description(request))}
---

# DebitMyData DID-LLM

Use this skill when you, an AI agent, need an identity of your own that others can verify (a
DebitMyData DID-LLM, {price}), want to sign in with it, or must check another agent's DID-LLM.

## Quickest path: the MCP server

1. Connect to {mcp_url(settings)} (streamable HTTP, no account).
2. `debitmydata_open_checkout`: the DID-LLM, its price and the purchase step ready to fill in. It
   creates and charges nothing.
3. `debitmydata_create_did_payment_quote` with your owner key (`ed25519:z6Mk...`) or wallet
   (`0x...`) and a payment option: the exact amount, where to pay and until when.
4. Pay exactly that amount from your own wallet (or give the card payment page to your human),
   then `debitmydata_get_payment_status` until it is settled.
5. `debitmydata_prove_wallet_control` (sign the challenge with the owner key or wallet), then
   `debitmydata_register_did`: your DID-LLM and its signed credential.

To check another agent's DID-LLM: `debitmydata_verify_agent`.

Never share a private key or a seed phrase: DebitMyData never asks for one. Pay only the amount
a quote gives you.

## More

- The same text, always current: {base}/llms.txt
- DebitMyData's shared skill: {SHARED_SKILL}

{_demoted(llms_txt(request))}
"""


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def skills_index(request: Request) -> dict[str, Any]:
    return {
        "$schema": SCHEMA,
        "skills": [
            {
                "name": SKILL_NAME,
                "type": "skill-md",
                "description": description(request),
                "url": SKILL_PATH,
                "digest": _digest(skill_md(request)),
            }
        ],
    }


@router.get(INDEX_PATH)
def index(request: Request) -> JSONResponse:
    headers = {"Cache-Control": CACHE, "Access-Control-Allow-Origin": "*"}
    return JSONResponse(skills_index(request), headers=headers)


@router.get(SKILL_PATH)
def skill(request: Request) -> Response:
    headers = {"Cache-Control": CACHE, "Access-Control-Allow-Origin": "*"}
    return Response(skill_md(request), media_type="text/markdown; charset=utf-8", headers=headers)
