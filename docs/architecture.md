# Architecture

## Purpose

The Agent Gateway lets any approved participant (autonomous agent, AI agent, bot,
agenticbot, claw, digital twin, person or organization) discover DebitMyData, pay a fixed
commercial price through an approved payment rail, prove control of its wallet or key, and
receive a DebitMyData identity, signed credential and entitlements.

The DebitMyData DID registration price is **25.00 USD**. The payer chooses an enabled rail;
the quote states the exact amount in that rail's asset.

## Components

```
                 ┌─────────────────────────────────────────────┐
 Agent / wallet  │               Agent Gateway                 │
 ─────────────►  │  REST API · MCP server · x402 / Bazaar      │
                 │                                             │
                 │  Quote + payment intent + policy service    │
                 │        │                                    │
                 │        ▼                                    │
                 │  Payment adapters (one per rail)            │
                 │   ├─ DMDNativePaymentAdapter ───────────────┼──► private DMD nodes
                 │   ├─ X402PaymentAdapter ────────────────────┼──► x402 facilitator
                 │   └─ Evm / Solana / future (disabled)       │
                 │        │                                    │
                 │        ▼                                    │
                 │  Settlement & finality worker               │
                 │        │                                    │
                 │        ▼                                    │
                 │  Identity + credential + entitlements       │
                 │        │                                    │
                 │        ▼                                    │
                 │  Append-only audit · reconciliation · alerts│
                 └─────────────────────────────────────────────┘
```

* **REST API and MCP** are two front doors to the same services. Each MCP tool calls the
  REST API in-process, so validation, issuance rules, audit and error codes are shared;
  settlement and issuance logic is never duplicated.
* **Payment adapters** hide each rail behind one interface
  (`src/dmd_gateway/adapters/base.py`) and report the normalized payment status.
* **The settlement worker** polls adapters in the background and records every status
  change in `payment_events`. HTTP requests never wait on chain confirmations.
* **Identity issuance** happens only through the rule in
  `src/dmd_gateway/domain/issuance.py`.

## Trust boundary

The gateway is the only public integration surface. Behind it, and never reachable by
callers:

* DMD Core RPC (private network only, dedicated credentials, method allowlist);
* node administration, mining controls, wallet unlock, key export;
* treasury and spending keys (kept in a separate environment);
* credential-signing keys (stored apart from public API hosts).

DMD payments are tracked with a watch-only / descriptor wallet so that gateway hosts never
hold keys that can move funds.

## Registration flow

1. `GET /v1/discovery` and `GET /v1/payment-options` describe the offer and the rails
   that are currently enabled.
2. `POST /v1/dids/registration-quotes` takes the display name, participant type,
   controller public key and/or controller wallet address, preferred payment option and
   metadata. It creates a registration intent and a payment intent for the chosen rail and
   returns the fixed 25.00 USD price, exact amount in the chosen asset, destination,
   finality policy, conversion source and time (non-USD assets), expiry and policy version.
   Non-USD amounts are rounded up to the next base unit and computed only from a price that
   passes the rail's price policy; otherwise the quote is refused. An `Idempotency-Key`
   header makes retries return the original quote instead of creating a new one.
3. The agent pays. It may report the transaction with
   `POST /v1/payments/{id}/references` (at most 5 per payment, stored append-only); this is
   only a hint for where to look, and the adapter always verifies on chain itself.
4. The worker moves the payment through `detected → confirming → verified → settled`.
5. The agent proves control of its controller key (before or after settlement):
   `POST /v1/dids/registrations/{id}/controller-challenges` returns a one-time message,
   and the agent sends its signature to `POST /v1/dids/registrations/{id}/controller-proofs`.
6. `POST /v1/dids/registrations` issues the identity once the issuance rule passes: the
   gateway creates the DID, signs the credential, grants entitlements and writes the audit
   trail in one transaction. `GET /v1/dids/registrations/{id}` shows what is still missing.

## Normalized payment status

| From | Allowed next states |
| --- | --- |
| created | awaiting_payment, expired, failed |
| awaiting_payment | detected, expired, failed |
| detected | confirming, underpaid, overpaid, reorged, failed |
| confirming | verified, reorged, failed |
| verified | settled, reorged, failed |
| settled | reorged, reversed, refunded, disputed |
| underpaid | refunded, failed |
| overpaid | confirming, refunded, failed |
| reorged | awaiting_payment, detected, confirming, failed |
| reversed | refunded, disputed |
| disputed | settled, reversed, refunded |
| expired | refunded |
| failed | refunded |
| refunded | (final) |

Payment policy (DEC-04): an underpayment can be topped up until the quote expires, then it
becomes `underpaid` and the amount received is due for refund; an overpayment is accepted
and the excess is due for refund; a payment first seen after the quote expired is never
credited and is due for refund. Nothing is ever credited silently. These rules live in
`src/dmd_gateway/domain/settlement.py` and apply to every rail.

## Native DMD payments

* The gateway's node wallet is a **watch-only descriptor wallet** holding only the public
  receive descriptor of the treasury wallet. The gateway can create addresses and see
  payments but can never spend; it refuses to run if its wallet has private keys.
* Each quote gets a fresh address, labelled with the payment-intent ID.
* The worker checks each payment with read-only RPC calls only: it decodes the raw
  transaction to verify the output and amount itself, checks that the confirming block is
  still in the active chain, and treats conflicting or replaced transactions as
  double-spends.
* Settlement after 6 confirmations credits each output once in `credited_outputs`.
* The gateway refuses to start if the node is on a different chain than the rail (for
  example a regtest node behind a mainnet rail).

The directive's example quotes show rail-specific statuses (`awaiting_authorization`,
`awaiting_dmd_payment`), while its normalized list has only `awaiting_payment`. Quotes
therefore report `payment_status: awaiting_payment` plus a `next_action`
(`authorize_x402_payment` or `send_payment`) until this is confirmed.

## Controller proof

The quote names the DID controller: an Ed25519 public key (`ed25519:` followed by a
Multikey `z6Mk...`, 64 hex characters or base64). Keys are checked at quote time, including
that they are valid curve points of large order, and stored in Multikey form.

* A challenge is a short text containing the gateway, the registration, the controller key,
  a random nonce and its validity window (5 minutes). The agent signs the exact UTF-8 bytes.
* A challenge can be answered once. A wrong signature uses it up, so it cannot be replayed
  or guessed; a malformed one (not 64 bytes of base64url/base64/hex) does not.
* At most 5 unanswered challenges can be open per registration.
* Every answer is stored in `controller_proofs`, which is append-only, and audited.
* A controller given only as a wallet address is refused at quote time for now: no wallet
  proof method exists yet (DEC-07), and a paid registration that can never be completed
  would only lead to a refund. The wallet address, when given with a key, is recorded but
  not bound to the identity, since it was not proven.

## MCP server

The gateway serves the directive's MCP tools at `/mcp` with the official MCP Python SDK,
over Streamable HTTP in stateless mode with JSON responses, so any instance can answer any
request. Discovery advertises the endpoint (`mcp_url`, which can be overridden when agents
reach it through another public address).

| Tool | REST call |
| --- | --- |
| `dmd_discover` | `GET /v1/discovery`, plus a short description of DebitMyData and DMD |
| `dmd_get_did_registration_offer` | the DID offer and `GET /v1/payment-options` |
| `dmd_list_supported_payment_options` | `GET /v1/payment-options` |
| `dmd_create_did_payment_quote` | `POST /v1/dids/registration-quotes` |
| `dmd_submit_payment_reference` | `POST /v1/payments/{id}/references` |
| `dmd_get_payment_status` | `GET /v1/payments/{id}` |
| `dmd_prove_wallet_control` | first call: `POST .../controller-challenges`; second call with the signature: `POST .../controller-proofs` |
| `dmd_register_did` | `POST /v1/dids/registrations` |
| `dmd_get_registration_status` | `GET /v1/dids/registrations/{id}` (added to the directive's list) |
| `dmd_catalog`, `dmd_get_purchase_quote` | the offers and their enabled payment options |
| `dmd_purchase` | for `did_registration`, the same as creating a quote |
| `dmd_marketplace_provider_requirements` | states that provider onboarding is not open yet |

Failed calls return the REST error body (`http_status`, `error.code`, details) as a tool
error. Requests whose `Host` or `Origin` is not the gateway's own address are refused
(DNS-rebinding protection); `DMD_GW_MCP_ALLOWED_HOSTS` adds host names when needed. Agents
sign challenges themselves: no tool takes or returns a private key.

## Identity issuance rule

An identity is issued only when **all** of these hold:

* payment status is `settled`;
* controller proof is `verified` for the registration's controller key;
* policy checks passed: the payment rail is still enabled (a killed rail also stops
  issuance), the settled outputs are credited to this payment, and the registration is not
  held for review. Registration quotas, anomaly detection and compliance screening are
  still to come (POL-*, DEC-10, DEC-11);
* the registration intent is valid (not expired, rejected or cancelled) and has not been
  used before.

Issuance is idempotent: repeating `POST /v1/dids/registrations` returns the identity that
was already issued; one registration intent produces at most one identity
(`identities.registration_intent_id` is unique), each identity is bound to the proof that
verified its controller (`identities.controller_proof_id`), and one payment output can be
credited to at most one intent (`credited_outputs` primary key). If a payment behind an
issued identity is later reorganized away, the worker records the alert
`alert.issued_identity_payment_reorged` for an operator.

## Identifiers, DID documents and credentials

* **DID (DEC-05):** `did:web:<gateway host>:agents:<id>`. `did:web` is an existing DID
  method, so standard resolvers fetch `https://<gateway host>/agents/<id>/did.json`, which
  lists the controller key (`Multikey`) for authentication and assertions. A deactivated
  identity answers 410.
* **Issuer:** `did:web:<gateway host>`, whose document at `/.well-known/did.json` lists the
  Ed25519 key that signs credentials.
* **Credential (DEC-06):** a W3C Verifiable Credential (data model 2.0) of type
  `DebitMyDataRegistrationCredential`, signed with a Data Integrity proof using the
  `eddsa-jcs-2022` cryptosuite (RFC 8785 canonical JSON, SHA-256, Ed25519). It is also the
  signed registration receipt: subject DID, display name, participant type, controller key
  and when it was verified, registration and payment intent, rail, asset, network, amount,
  settled outputs, commercial price, terms version and entitlements.
  `scripts/demo_agent.py verify-credential` checks one using only public information.
* **Entitlements:** `dmd_did_holder` is granted with the identity; product and marketplace
  eligibility come later (ID-11).
* **Issuer key custody (ID-15):** development and staging read the key from a file only its
  owner can read (`DMD_GW_ISSUER_KEY_PATH`, created with `dmd-gateway-issuer-key`).
  Production refuses a key file: it needs a separate signer so the key never sits on a
  public API host.

## Identifier wording

The product is "DebitMyData Agentic DID Registration", and public descriptions use the
wording set in the directive. Identities are standard `did:web` identifiers. Do not claim a
`did:dmd` method until its method specification, resolution rules and governance are
published.
