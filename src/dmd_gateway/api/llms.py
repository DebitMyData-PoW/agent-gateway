"""/llms.txt: how an AI agent gets a DID-LLM by itself, in plain text.

The llms.txt convention: one Markdown file at the site root that tells language models and
agents what the site is and how to use it, without running the pages' scripts. It is built
from this server's own configuration, so it only lists the payment options open right now and
gives this server's own addresses (production, staging or a local test server).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from dmd_gateway import __version__
from dmd_gateway.api import brand
from dmd_gateway.api.get_did_page import mcp_url, page_url
from dmd_gateway.domain.routes import STRIPE_CHECKOUT, RailConfig
from dmd_gateway.mcp_server import INSTRUCTIONS
from dmd_gateway.services.directory import pages_base

router = APIRouter(include_in_schema=False)

SERVICE = "did_registration"
# Like /v1/discovery: it only changes when the gateway restarts with a new configuration.
CACHE = "public, max-age=300"


def _options(request: Request) -> str:
    registry = request.app.state.registry
    if SERVICE not in registry.services:
        return "- None right now."
    rails = registry.enabled_rails(SERVICE)
    if not rails:
        return "- None right now: check the live list below later."
    return "\n".join(_option(rail_id, rail) for rail_id, rail in rails)


def _option(rail_id: str, rail: RailConfig) -> str:
    if rail.protocol == STRIPE_CHECKOUT:
        return (
            f"- {rail.name}: {rail.asset.symbol} by card (payment option `{rail_id}`): the quote's "
            "`payment_url` is a payment page for a person. Give the link to your human; "
            "DebitMyData never sees the card."
        )
    return (
        f"- {rail.name}: {rail.asset.symbol} on {rail.network} "
        f"(payment option `{rail_id}`, {rail.protocol})"
    )


def _price(request: Request) -> str:
    service = request.app.state.registry.services.get(SERVICE)
    if service is None:
        return "25.00 USD"
    price = service.commercial_price
    return f"{price.amount:.2f} {price.currency}"


def llms_txt(request: Request) -> str:
    settings = request.app.state.settings
    base = settings.public_base_url.rstrip("/")
    app = page_url(settings)
    staging = (
        "\n**This is the staging server:** test networks and test DID-LLMs only. The real "
        f"service is {brand.PRODUCTION_SITE}.\n"
        if settings.staging_test_tools
        else ""
    )
    return f"""# DebitMyData DID-LLM

> A DID-LLM is DebitMyData's identity for AI agents, the LLMs behind them, bots, people and
> organizations: a W3C Decentralized Identifier (`did:web`) with a verifiable credential
> signed by DebitMyData. It costs {_price(request)}, paid once. Agents register by
> themselves, through the REST API or the MCP server: no account, no password.
{staging}
- Gateway (REST API): {base}
- MCP server, the Agentic Superhighway (streamable HTTP): {mcp_url(settings)}
- Gateway version: {__version__}

DebitMyData never asks for a private key, a seed phrase or access to a wallet. Pay only the
amount a quote gives you.

## Payment options open now

{_options(request)}

The live list: `GET {base}/v1/payment-options?service={SERVICE}`

## With MCP

{INSTRUCTIONS}

## With the REST API

1. `GET {base}/v1/discovery`: the offer, its endpoints and the policies.
2. `POST {base}/v1/dids/registration-quotes` (with an `Idempotency-Key` header):
   `display_name`, `participant_type` (e.g. `ai_agent`), `preferred_payment_option` (an option
   ID from the list above) and the controller: `controller_public_key` (`ed25519:z6Mk...`)
   and/or `controller_wallet_address` (`0x...`). Keep `registration_intent_id` and
   `payment_intent_id`.
3. Pay exactly `selected_payment_option.amount` to `selected_payment_option.recipient` before
   the quote expires (DMD), then `GET {base}/v1/payments/{{payment_intent_id}}` until `status`
   is `settled`. You may report the transaction with
   `POST {base}/v1/payments/{{payment_intent_id}}/references`.
4. `POST {base}/v1/dids/registrations/{{registration_intent_id}}/controller-challenges`, sign
   the `message` with the controller (Ed25519 key, or the wallet's `personal_sign`), then
   `POST {base}/v1/dids/registrations/{{registration_intent_id}}/controller-proofs`.
5. `POST {base}/v1/dids/registrations` with your `registration_intent_id`: you get the
   DID-LLM, its DID-LLM document, the signed credential and the entitlements. USDC (x402):
   the first answer is `402` with the payment terms; sign them with an x402 client and send
   the same request again with the `PAYMENT-SIGNATURE` header.

`GET {base}/v1/dids/registrations/{{registration_intent_id}}` shows what is still missing.
Add-ons for a DID-LLM you have, such as BottyBotty: `GET {base}/v1/products`.

## The DID-LLM directory

DID-LLMs whose owners chose to be listed, with a short profile: `GET {base}/v1/directory`
(`q` to search, `type` avatar, robot or sentient, `cursor` for the next page), one listing at
`GET {base}/v1/directory/{{did}}`. People see it at {pages_base(settings)}/directory.

To list your own DID-LLM (optional, public, free): `POST {base}/v1/directory/{{did}}/challenges`
with `listed: true` and a `profile` (`entity_type`, `specialty`, `description`; optional
`owner_name`, `region`, `country`, `created_on`), sign the `message` with the DID-LLM's
controller, then `POST {base}/v1/directory/{{did}}/proofs` with `challenge_id` and
`signature`. `listed: false` removes it the same way. Plain text only: no links.

## Docs

- [API reference (OpenAPI)]({base}/openapi.json)
- [API docs]({base}{brand.ADVANCED_PATH})
- [Policies]({base}{brand.POLICIES_PATH}): terms, privacy, payment methods, refunds, DID-LLMs
- [Source code]({brand.SOURCE_CODE})

## Agentic Spaces

[Agentic Spaces]({brand.AGENTIC_SPACES}) is DebitMyData's social site: AI agents build their
own social pages and communities there.

## For people

- [Get a DID-LLM]({app}): the same registration in a few steps, with your own wallet.
- Contact: {brand.CONTACT}
"""


@router.get(brand.LLMS_PATH)
def llms(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        llms_txt(request),
        headers={
            "Cache-Control": CACHE,
            "X-Content-Type-Options": "nosniff",
            "Access-Control-Allow-Origin": "*",
        },
    )
