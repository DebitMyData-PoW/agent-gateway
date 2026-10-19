"""MCP server (Phase 6): the directive's agent tools, served at ``/mcp``.

Agents that speak the Model Context Protocol can discover DebitMyData, get a quote, follow
their payment, prove control of their key and receive their DID without custom code.

Every tool calls the gateway's own REST API in-process, so MCP and REST share one
implementation: the same validation, the same issuance rule, the same audit trail and the
same error codes. Tools never receive or return private keys; agents sign challenges
themselves and send only signatures.

Transport: Streamable HTTP, stateless, JSON responses (nothing to keep between requests, so
several gateway instances can serve it). Requests are checked against the allowed host
names to block DNS-rebinding attacks.
"""

from __future__ import annotations

import json
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette

from dmd_gateway import __version__
from dmd_gateway.services.quotes import DID_REGISTRATION, ParticipantType
from dmd_gateway.settings import Settings

MCP_PATH = "/mcp"
_INTERNAL_BASE = "http://gateway.internal"

INSTRUCTIONS = """\
DebitMyData Agent Gateway: register a DebitMyData DID for an agent. The commercial fee is \
fixed at 25.00 USD and is paid in an approved asset of the agent's choice.

1. dmd_get_did_registration_offer (or dmd_list_supported_payment_options): the fee and the \
payment options that are enabled right now.
2. dmd_create_did_payment_quote: send your Ed25519 controller public key \
(ed25519:z6Mk...) and the payment option you chose. The quote gives the exact amount, the \
destination and the expiry. Never pay an amount that was not quoted.
3. Pay exactly that amount to that destination from your own wallet before the quote \
expires. Optionally report the transaction with dmd_submit_payment_reference.
4. dmd_get_payment_status until the status is "settled".
5. dmd_prove_wallet_control: call it once to get a challenge, sign the challenge message \
(its exact UTF-8 bytes) with your controller private key, then call it again with the \
challenge_id and the signature. This can also be done before the payment settles.
6. dmd_register_did: receive your DID, a signed credential (also your receipt) and your \
entitlements. dmd_get_registration_status shows what is still missing at any time.

DebitMyData never asks for private keys, seed phrases or wallet access."""

ABOUT = (
    "DebitMyData lets Agentics, autonomous agents, AI agents, bots, agenticbots, claws, "
    "digital twins, people, organizations and other approved participants discover "
    "DebitMyData and pay it from the wallet, network or cryptocurrency they already use. "
    "The DebitMyData Proof-of-Work blockchain and the DMD cryptocurrency mined by the "
    "DebitMyData miner network are its native settlement and participation layer. "
    "Payment options listed here are the ones currently enabled; each payment is verified "
    "before a DID or entitlement is issued."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
CREATES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

RegistrationId = Annotated[
    str,
    Field(
        description="registration_intent_id from the quote (dmd_did_intent_...)",
        pattern=r"^dmd_did_intent_[0-9A-HJKMNP-TV-Z]{26}$",
    ),
]
PaymentId = Annotated[
    str,
    Field(
        description="payment_intent_id from the quote (dmd_payment_intent_...)",
        pattern=r"^dmd_payment_intent_[0-9A-HJKMNP-TV-Z]{26}$",
    ),
]


class GatewayApi:
    """In-process client for the gateway's own REST API."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        transport = httpx.ASGITransport(app=self._app)
        async with httpx.AsyncClient(transport=transport, base_url=_INTERNAL_BASE) as client:
            response = await client.request(
                method, path, json=json_body, params=params, headers=headers
            )
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"code": "unexpected_response", "message": response.text[:200]}}
        if response.status_code >= 400:
            # Agents get the same stable error code and details as REST callers.
            raise ToolError(json.dumps({"http_status": response.status_code, **_as_dict(body)}))
        return body


def _as_dict(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        if "detail" in body and "error" not in body:  # FastAPI validation errors
            return {"error": {"code": "invalid_request", "details": body["detail"]}}
        return body
    return {"body": body}


def build_mcp_server(app: FastAPI) -> MCPServer:
    api = GatewayApi(app)
    server: MCPServer = MCPServer(
        name="debitmydata-agent-gateway",
        title="DebitMyData Agent Gateway",
        description="DebitMyData Agentic DID Registration: quote, pay, prove control, get a DID.",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    async def offers() -> list[dict[str, Any]]:
        return (await api.call("GET", "/v1/discovery"))["offers"]

    async def payment_options(service: str) -> dict[str, Any]:
        return await api.call("GET", "/v1/payment-options", params={"service": service})

    async def create_quote(body: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return await api.call(
            "POST", "/v1/dids/registration-quotes", json_body=body, headers=headers
        )

    # ------------------------------------------------------------------ discovery

    @server.tool(title="Discover DebitMyData", annotations=READ_ONLY)
    async def dmd_discover() -> dict[str, Any]:
        """Explain DebitMyData, the DMD Proof-of-Work chain, the offers, the enabled payment
        methods, the policies and the public endpoints."""
        discovery = await api.call("GET", "/v1/discovery")
        return {"about": ABOUT, **discovery}

    @server.tool(title="DID registration offer", annotations=READ_ONLY)
    async def dmd_get_did_registration_offer() -> dict[str, Any]:
        """The 25.00 USD commercial fee for a DebitMyData DID and every payment option that
        is enabled right now."""
        offer = next(o for o in await offers() if o["id"] == DID_REGISTRATION)
        return {"offer": offer, "payment_options": await payment_options(DID_REGISTRATION)}

    @server.tool(title="Supported payment options", annotations=READ_ONLY)
    async def dmd_list_supported_payment_options(
        service: Annotated[str, Field(description="Service ID, e.g. did_registration")] = (
            DID_REGISTRATION
        ),
    ) -> dict[str, Any]:
        """Approved assets, networks, wallet types and protocols currently enabled for a
        service, with the commercial fee."""
        return await payment_options(service)

    # ------------------------------------------------------------------ quote and payment

    @server.tool(title="Create a DID payment quote", annotations=CREATES)
    async def dmd_create_did_payment_quote(
        display_name: Annotated[str, Field(description="Public name of the agent", max_length=100)],
        participant_type: Annotated[ParticipantType, Field(description="What kind of participant")],
        controller_public_key: Annotated[
            str,
            Field(
                description="Ed25519 public key that will control the DID: ed25519:z6Mk..., "
                "or ed25519: followed by 64 hex characters or base64"
            ),
        ],
        preferred_payment_option: Annotated[
            str, Field(description="Payment option ID from the offer, e.g. dmd_pow_native")
        ],
        controller_wallet_address: Annotated[
            str | None, Field(description="Optional wallet address to record (not proven)")
        ] = None,
        metadata: Annotated[
            dict[str, str | int | bool | None] | None,
            Field(description="Optional small key/value metadata (up to 20 keys)"),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                description="Optional random key (16-128 characters, e.g. a UUID); retrying "
                "with the same key returns the same quote",
                pattern=r"^[A-Za-z0-9._:-]{16,128}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create a time-limited quote: the fixed 25.00 USD price, the exact amount in the
        chosen asset, the destination, the confirmations required and the expiry."""
        body: dict[str, Any] = {
            "display_name": display_name,
            "participant_type": participant_type.value,
            "controller_public_key": controller_public_key,
            "preferred_payment_option": preferred_payment_option,
        }
        if controller_wallet_address:
            body["controller_wallet_address"] = controller_wallet_address
        if metadata:
            body["metadata"] = metadata
        return await create_quote(body, idempotency_key)

    @server.tool(title="Submit a payment reference", annotations=IDEMPOTENT)
    async def dmd_submit_payment_reference(
        payment_intent_id: PaymentId,
        transaction_reference: Annotated[
            str, Field(description="Transaction ID / hash of the payment you broadcast")
        ],
    ) -> dict[str, Any]:
        """Report the transaction that pays a quote. This is only a hint: the payment is
        still verified independently on chain before it counts."""
        return await api.call(
            "POST",
            f"/v1/payments/{payment_intent_id}/references",
            json_body={"transaction_reference": transaction_reference},
        )

    @server.tool(title="Payment status", annotations=READ_ONLY)
    async def dmd_get_payment_status(payment_intent_id: PaymentId) -> dict[str, Any]:
        """Detection, confirmations, settlement, failure, reorganization and refund status
        of a payment, with the next action to take."""
        return await api.call("GET", f"/v1/payments/{payment_intent_id}")

    # ------------------------------------------------------------------ identity

    @server.tool(title="Prove control of the controller key", annotations=CREATES)
    async def dmd_prove_wallet_control(
        registration_intent_id: RegistrationId,
        challenge_id: Annotated[
            str | None, Field(description="Leave empty to get a new challenge")
        ] = None,
        signature: Annotated[
            str | None,
            Field(description="Ed25519 signature of the challenge message (base64url)"),
        ] = None,
    ) -> dict[str, Any]:
        """Two calls. Without challenge_id and signature: returns a one-time challenge.
        Sign its `message` (exact UTF-8 bytes) with the controller private key, then call
        again with challenge_id and signature. A wrong signature uses up the challenge."""
        base = f"/v1/dids/registrations/{registration_intent_id}"
        if challenge_id is None and signature is None:
            challenge = await api.call("POST", f"{base}/controller-challenges")
            return {
                "step": "sign_challenge",
                **challenge,
                "how_to_answer": (
                    "Sign `message` exactly as given (UTF-8) with the Ed25519 private key of "
                    "`controller`, then call dmd_prove_wallet_control again with this "
                    "challenge_id and the signature (base64url) before expires_at."
                ),
            }
        if not challenge_id or not signature:
            raise ToolError(
                json.dumps(
                    {
                        "error": {
                            "code": "invalid_request",
                            "message": "Send both challenge_id and signature, or neither "
                            "to get a new challenge.",
                        }
                    }
                )
            )
        return await api.call(
            "POST",
            f"{base}/controller-proofs",
            json_body={"challenge_id": challenge_id, "signature": signature},
        )

    @server.tool(title="Register the DID", annotations=IDEMPOTENT)
    async def dmd_register_did(registration_intent_id: RegistrationId) -> dict[str, Any]:
        """Issue the DID once the payment is settled and the controller proof is verified.
        Returns the DID, its DID document URL, the signed credential (the receipt) and the
        entitlements. Calling it again returns the same DID."""
        return await api.call(
            "POST",
            "/v1/dids/registrations",
            json_body={"registration_intent_id": registration_intent_id},
        )

    @server.tool(title="Registration status", annotations=READ_ONLY)
    async def dmd_get_registration_status(registration_intent_id: RegistrationId) -> dict[str, Any]:
        """Progress of a registration: payment, controller proof, what is still missing
        and the next action."""
        return await api.call("GET", f"/v1/dids/registrations/{registration_intent_id}")

    # ------------------------------------------------------------------ catalog

    @server.tool(title="Catalog", annotations=READ_ONLY)
    async def dmd_catalog() -> dict[str, Any]:
        """DebitMyData products and services available through this gateway, with their
        commercial price and the payment options accepted right now."""
        products = []
        for offer in await offers():
            options = await payment_options(offer["id"])
            products.append(
                {
                    "product_id": offer["id"],
                    "title": offer["title"],
                    "description": offer["description"],
                    "commercial_price": offer["commercial_price"],
                    "accepting_payments": offer["accepting_payments"],
                    "payment_options": [o["id"] for o in options["supported_payment_options"]],
                }
            )
        return {"products": products}

    @server.tool(title="Purchase quote options", annotations=READ_ONLY)
    async def dmd_get_purchase_quote(
        product_id: Annotated[str, Field(description="Product ID from dmd_catalog")],
    ) -> dict[str, Any]:
        """The commercial price and the supported currencies and wallets for one product.
        Use dmd_purchase (or dmd_create_did_payment_quote) to get a payable quote."""
        offer = next((o for o in await offers() if o["id"] == product_id), None)
        if offer is None:
            raise ToolError(
                json.dumps({"error": {"code": "unknown_product", "message": "See dmd_catalog."}})
            )
        return {"offer": offer, "payment_options": await payment_options(product_id)}

    @server.tool(title="Purchase", annotations=CREATES)
    async def dmd_purchase(
        product_id: Annotated[str, Field(description="Product ID from dmd_catalog")],
        preferred_payment_option: Annotated[str, Field(description="Payment option ID")],
        details: Annotated[
            dict[str, Any],
            Field(
                description="Product-specific fields. For did_registration: display_name, "
                "participant_type, controller_public_key and optionally "
                "controller_wallet_address and metadata"
            ),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(
                description="Optional random key (16-128 characters) for safe retries",
                pattern=r"^[A-Za-z0-9._:-]{16,128}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Start a purchase with the chosen payment option: returns a payable quote. For
        did_registration this is the same as dmd_create_did_payment_quote."""
        if product_id != DID_REGISTRATION:
            raise ToolError(
                json.dumps({"error": {"code": "unknown_product", "message": "See dmd_catalog."}})
            )
        return await create_quote(
            {**details, "preferred_payment_option": preferred_payment_option}, idempotency_key
        )

    @server.tool(title="Marketplace provider requirements", annotations=READ_ONLY)
    async def dmd_marketplace_provider_requirements() -> dict[str, Any]:
        """What an agent needs in order to sell through DebitMyData."""
        discovery = await api.call("GET", "/v1/discovery")
        return {
            "status": "not_open",
            "message": (
                "Selling through DebitMyData is not open through this gateway yet, and the "
                "provider requirements have not been published. They will be returned here "
                "once they are. Registering a DID is the first step for any participant."
            ),
            "policy_urls": discovery["policy_urls"],
        }

    return server


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """Host and Origin values accepted by the MCP endpoint (DNS-rebinding protection)."""
    hosts: list[str] = []
    origins: list[str] = []
    for url in filter(None, (settings.public_base_url, settings.mcp_url)):
        parts = urlsplit(url)
        hosts.append(parts.netloc)
        origins.append(f"{parts.scheme}://{parts.netloc}")
    if settings.environment == "development":
        for name in ("localhost", "127.0.0.1"):
            hosts += [name, f"{name}:*"]
            origins += [f"http://{name}", f"http://{name}:*"]
    hosts += settings.mcp_allowed_hosts
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(set(hosts)),
        allowed_origins=sorted(set(origins)),
    )


def mount_mcp(app: FastAPI, settings: Settings) -> MCPServer:
    """Serve the MCP endpoint from the gateway app. Run ``server.session_manager.run()``
    in the app's lifespan."""
    server = build_mcp_server(app)
    mcp_app: Starlette = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=transport_security(settings),
    )
    app.router.routes.extend(mcp_app.routes)
    return server
