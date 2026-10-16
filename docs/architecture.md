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

* **REST API and MCP** are two front doors to the same services. MCP tools call the same
  code as REST endpoints; settlement and issuance logic is never duplicated.
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
3. The agent pays. For native chains it may submit the transaction reference as a hint;
   the adapter always verifies on chain itself.
4. The worker moves the payment through `detected → confirming → verified → settled`.
5. The agent answers a one-time controller challenge to prove control of its wallet/key.
6. When the issuance rule passes, the gateway creates the identity, signs the credential,
   grants entitlements and writes the audit trail in the same transaction.

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

Underpaid, overpaid and late payments only lead to refund or failure until the refund
policy (DEC-04) is decided. Nothing is ever credited silently.

The directive's example quotes show rail-specific statuses (`awaiting_authorization`,
`awaiting_dmd_payment`), while its normalized list has only `awaiting_payment`. Quotes
therefore report `payment_status: awaiting_payment` plus a `next_action`
(`authorize_x402_payment` or `send_payment`) until this is confirmed.

## Identity issuance rule

An identity is issued only when **all** of these hold:

* payment status is `settled`;
* controller proof is `verified`;
* rate-limit, fraud, policy and compliance checks passed;
* the registration intent was valid when paid and has not been used before.

Issuance is idempotent: one registration intent produces at most one identity
(`identities.registration_intent_id` is unique), and one payment output can be credited to
at most one intent (`credited_outputs` primary key).

## Identifier wording

The product is "DebitMyData Agentic DID Registration", and public descriptions use the
wording set in the directive. One caution: `did:dmd` is not yet a published DID method, so
until its method specification, resolution rules and governance are published, do not
claim that DebitMyData DIDs resolve or interoperate as a standard W3C DID method.
