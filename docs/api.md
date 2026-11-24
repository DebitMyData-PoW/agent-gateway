# The registration flow: REST API and MCP tools

| Step | Endpoint |
| --- | --- |
| Discover the offer and enabled payment options | `GET /v1/discovery`, `GET /v1/payment-options` |
| Get a quote (fixed 25.00 USD, exact amount in the chosen asset) | `POST /v1/dids/registration-quotes` |
| Pay (DMD), then follow the payment | `GET /v1/payments/{payment_intent_id}` |
| Get a one-time message to sign with the controller key or wallet | `POST /v1/dids/registrations/{registration_intent_id}/controller-challenges` |
| Send the signature | `POST /v1/dids/registrations/{registration_intent_id}/controller-proofs` |
| See what is still missing | `GET /v1/dids/registrations/{registration_intent_id}` |
| Receive the DID-LLM, signed credential and entitlements (x402: pay here, see below) | `POST /v1/dids/registrations` |
| Optionally report the payment transaction (a hint only) | `POST /v1/payments/{payment_intent_id}/references` |
| Look them up | `GET /v1/dids/{identity_id}`, `GET /v1/credentials/{credential_id}`, `GET /v1/entitlements/{identity_id}` (the identity ID or the DID-LLM itself) |
| Resolve the DID-LLM and the issuer key | `GET /agents/{id}/did.json`, `GET /.well-known/did.json` |
| Read the terms, privacy, payment, refund, DID-LLM and security policies | `/policies`, `GET /v1/policies` |
| Optional: list the DID-LLM in the directory (signed by its owner), or read the directory | `POST /v1/directory/{did}/challenges`, `POST /v1/directory/{did}/proofs`, `GET /v1/directory` |

The same steps are available as MCP tools at `/agenticsuperhighway` (Streamable HTTP; `/mcp`,
its first address, still works): `debitmydata_discover`,
`debitmydata_get_did_registration_offer`, `debitmydata_open_checkout`,
`debitmydata_list_supported_payment_options`, `debitmydata_create_did_payment_quote`,
`debitmydata_submit_payment_reference`, `debitmydata_get_payment_status`,
`debitmydata_prove_wallet_control`, `debitmydata_register_did`,
`debitmydata_get_registration_status`, `debitmydata_catalog`, `debitmydata_get_purchase_quote`,
`debitmydata_purchase`, `debitmydata_marketplace_provider_requirements`,
`debitmydata_search_directory`, `debitmydata_list_in_directory`, `debitmydata_verify_agent`,
`debitmydata_change_owner` and `debitmydata_revoke_did`. Each tool calls the REST API above
in-process, so both give the same results and error codes. Its Server Card,
`/agenticsuperhighway/server-card` (listed in `/.well-known/ai-catalog.json`), tells MCP clients
and registries what it is and how to connect.

## Verifying an agent

**Verifying an agent by its DID-LLM** (the runbook's `verify_agent`; MCP
`debitmydata_verify_agent`). Any agent or service checks a DID-LLM it meets:
`POST /v1/dids/verifications` with `did` (and `audience`, its own name) answers whether
DebitMyData issued it and it is active, its credential checked against DebitMyData's issuer key,
its owner key or wallet, its entitlements, and a `challenge`: a "DebitMyData proof of control"
message, valid 10 minutes, naming the DID-LLM, the verifier and a nonce. The agent signs it with
its DID-LLM's key or wallet; the same request with `message` and `signature` adds `proof`
(whether the signature is the owner's now, with the nonce to compare). `verified` sums it up,
`reasons` says why not. Only messages in this form are checked, so a signature made to sign in
to DebitMyData is never taken as a proof for someone else.

## Inference donations

**Inference donations (agents).** `POST /v1/donations/inference` with
`{"amount_usd": "5.00"}` (from 0.50 to 1,000.00 USD; optionally `donor_name` and
`donor_did`) answers `402` with the x402 terms for exactly that amount, paid to
DebitMyData's inference wallet (`DMD_USDC_BASE_INFERENCE_ADDRESS`, not the DID-LLM payment
address). Send the same body again with the signed payment in `PAYMENT-SIGNATURE`: no quote
is needed, the payment is checked on chain like a DID-LLM payment, and the answer (`201`) carries
a receipt signed by DebitMyData (a `DebitMyDataDonationReceipt` credential), or `202` with a
`status_url` (`GET /v1/donations/{purchase_id}`) while confirmations come in. Sending the
same payment again returns the same donation. Through MCP: `debitmydata_catalog` lists
`inference_donation`, and `debitmydata_purchase` takes the amount in `details` and the payment in
`x402_payment` or `_meta["x402/payment"]`. Donations appear under `donations` in
`/v1/discovery`.

## Paying with x402

**Paying with x402 (USDC on Base).** Quote with `"preferred_payment_option": "x402_usdc_base"`,
prove the controller key, then call `POST /v1/dids/registrations`. It answers `402` with a
`PAYMENT-REQUIRED` header (x402 version 2, scheme `exact`: the quoted amount in USDC base units,
the USDC contract, the network and DebitMyData's receiving address, plus a Bazaar discovery
description). Call it again with the signed payment in the `PAYMENT-SIGNATURE` header: the
gateway checks it against the quote, the facilitator verifies and settles it, the gateway checks
the settlement on chain itself, and the DID-LLM comes back in the same answer with a
`PAYMENT-RESPONSE` header (or `202` if the confirmations take longer; then call again without
paying). Payment is asked for only once everything else is ready, so an agent is never charged
for a DID-LLM that cannot be issued. Any standard x402 client can pay it; through MCP,
`debitmydata_register_did` returns the payment requirements and takes the payment in
`x402_payment` or `_meta["x402/payment"]`.

See [architecture.md](architecture.md) for the rules behind each step, and
[integrations.md](integrations.md) to connect a product.
