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
                 │   ├─ DMDNativePaymentAdapter ───────────────┼──► private DebitMyData nodes
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

* DebitMyData Core RPC (private network only, dedicated credentials, method allowlist);
* node administration, mining controls, wallet unlock, key export;
* treasury and spending keys (kept in a separate environment);
* the credential-signing key (in production, held only by the credential signer: no network,
  its own user, checks every request against the database).

DMD payments are tracked with a watch-only / descriptor wallet so that gateway hosts never
hold keys that can move funds.

## DMD/USD price

Until DMD has a market price, DMD is priced at the cost of mining it (DEC-01, approved as
the interim method): the electricity it takes to mine a block, divided by the DMD the block
creates. Mining a block takes `difficulty × 2^32` hashes on average, so:

```text
USD per DMD = hashes per block × joules per hash × USD per joule ÷ DMD per block
```

The payment worker computes it every 5 minutes (`pricing/mining_cost.py`) and stores it,
append-only, with every input:

* **hashes per block**: from the gateway's own DebitMyData mainnet node, averaged over the last week
  (4,032 blocks) from the chain's total work, so a short burst of extra hashing power barely
  moves it, and cross-checked against the public explorer. Servers without a mainnet node
  (staging) use the explorer's current difficulty instead (`DMD_GW_DMD_PRICE_CHAIN_DATA_FROM`);
* **DMD per block**: the chain's rule at the current height (35 DMD, halving every 300,000
  blocks);
* **machine efficiency and electricity price**: approved values in
  `config/payment_routes.yaml` (`dmd_mining_cost`: 20 J/TH and 0.06 USD/kWh, reviewed every
  quarter). The electricity price is what miners typically pay (industrial rates where
  mining takes place), not a world average of household prices. An overdue review is logged
  and shown with the price.

Nothing comes from another blockchain or from an outside price service. Quotes use the
latest stored price and refuse when it is older than 15 minutes; no quote waits for a node
or a website. `GET /v1/prices/dmd-usd` shows the price, its inputs and its age. The price
follows the network's work: more miners, a higher price. It is a reference price, not a
market price.

**Minimum rate.** While DMD is young and its mining power small, its mining cost is far
below any useful rate (about 0.000065 USD per DMD in October 2026: a 25.00 USD DID would cost
about 386,000 DMD, 8% of all DMD mined). The price is therefore the higher of a minimum rate
set by DebitMyData (`minimum_usd_per_dmd` in `dmd_mining_cost`, 0.001 USD per DMD: 25,000
DMD for a DID) and the mining cost. The mining cost keeps being computed and is shown next to
the minimum, with the one that applied (`price_from`). Because the work per block is
averaged over a week, a short burst of hashing power cannot lift the price above the
minimum: that takes about 15 times today's network work for most of a week. DebitMyData
approved the rate on 27 October 2026 (`minimum_approved_on`); the date is shown with the
price. Once DMD trades on an exchange, a market price source replaces both.

## Monitoring

`dmd-gateway-monitor` (`monitoring/`) runs every 2 minutes from a systemd timer. It checks the
API, the payment worker's heartbeat (recorded on every pass), each enabled payment option's
readiness (node, chain RPC, facilitator), the DMD price, the DebitMyData node against the public
explorer, alerts recorded by the worker (a payment that left the chain after issuance),
refunds due, stuck payments, and whether a receiving destination changed. Findings are kept
in `alert_states`: an ongoing problem is sent when it starts, repeated while it lasts, and
sent again when resolved; one-off events are sent once. Messages go by email (SMTP with TLS)
and to a Discord webhook, critical ones also to the critical-only recipients and with a
mention. Every day a reconciliation report checks the database against itself and every
payment settled in the last 30 days again on its chain, read-only. See
[monitoring.md](monitoring.md).

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
   A controller key may hold at most 5 open quotes at a time (wallets are not counted: a
   wallet address is public, so anyone could use up a wallet's quotes).
3. The agent pays. It may report the transaction with
   `POST /v1/payments/{id}/references` (at most 5 per payment, stored append-only); this is
   only a hint for where to look, and the adapter always verifies on chain itself.
4. The worker moves the payment through `detected → confirming → verified → settled`.
5. The agent proves control of each controller it named, its key and/or its wallet (before
   or after settlement): `POST /v1/dids/registrations/{id}/controller-challenges` returns a
   one-time message, and the agent sends its signature to
   `POST /v1/dids/registrations/{id}/controller-proofs`.
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

DMD is the ticker of the DebitMyData coin, the native coin of the DebitMyData Proof-of-Work
chain, as every DebitMyData wallet shows it. Quotes give the asset as `DMD` and name the
payment option "DebitMyData coin (native PoW chain)"; the ticker is explained wherever an
agent first meets it, since unrelated tokens use the same ticker.

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
(`prove_wallet_control` for x402, `send_payment` for DMD) until this is confirmed.

## x402 payments (USDC on Base)

x402 payments follow x402 protocol version 2, scheme `exact` with EIP-3009
`transferWithAuthorization`, on the registration route itself:

1. The quote fixes the amount (25.000000 USDC), the USDC contract, the network and the
   receiving address (one merchant address from the environment, stored with the quote).
2. Once the controller is proven, `POST /v1/dids/registrations` answers `402` with the
   payment requirements in a `PAYMENT-REQUIRED` header, plus the `bazaar` discovery
   description. Nothing is asked for while the DID could not be issued (controller not
   proven, intent closed, issuance off), so no agent pays for a DID it cannot get.
3. The agent signs and calls again with `PAYMENT-SIGNATURE`. The gateway checks the signed
   authorization against the quote (amount, recipient, asset, network, validity window),
   stores it in `x402_authorizations` (one live authorization per intent; an authorization
   never counts for two intents), has the facilitator verify it, records that it is about
   to settle, then has the facilitator settle it. The gateway never signs or sends anything.
4. The adapter then checks the chain itself through read-only RPC: the transaction
   succeeded, contains the USDC contract's `AuthorizationUsed` event for exactly the payer
   and nonce received for this intent and the `Transfer` to the intent's receiving address,
   and its block is still in the chain. If the facilitator's answer is lost, the same event
   finds the payment. The common settlement rules then apply (5 confirmations on Base,
   DEC-16), the transfer is credited once (transaction and log index), and the DID is
   issued in the same answer with a `PAYMENT-RESPONSE` header, or `202` while confirming.

Because the receiving address is shared, a transfer counts for an intent only through the
authorization received for that intent; transaction references sent by payers are refused
on this rail.

**Facilitators.** Staging uses the public x402.org test facilitator. On Base mainnet the
gateway uses Coinbase's CDP facilitator (`https://api.cdp.coinbase.com/platform/v2/x402`),
which is also what lists paid endpoints in the x402 Bazaar. Each request to it carries a
JWT valid for 2 minutes and naming that one request, signed with DebitMyData's CDP API key
(`adapters/cdp_auth.py`, the same token as Coinbase's SDK); the key secret is read from the
environment only.

**Bazaar listing.** x402 tools probe a paid route before they know its input: Coinbase's
validator posts an empty body, then the example request advertised in the `bazaar`
extension, which uses a reserved `registration_intent_id` (`dmd_did_intent_` followed by 26
zeros, a time no real quote can have). A registration call without a quote, or with that
example, answers `402` with the real terms (25.000000 USDC, Base, the receiving address) and
is never paid: a payment sent with it is refused before the facilitator is called. Every
402 also carries the PaymentRequired fields (`x402Version`, `resource`, `accepts`,
`extensions`) at the top of its body, next to the gateway's `error` object, for clients
that read the body instead of the `PAYMENT-REQUIRED` header.
`scripts/validate_bazaar.py` runs Coinbase's validator against a deployed gateway; the
listing itself follows the first payment settled through the CDP facilitator.

## Inference donations

People donate towards DebitMyData's AI inference on debitmydata.com (the "Inference $"
button, PayPal); that is unchanged. Agents and other x402 wallets donate at
`POST /v1/donations/inference` (`services/donations.py`), defined under `donations` in the
route registry:

1. There is no quote: the donor names `amount_usd`, within the registry's range (0.50 to
   1,000.00 USD). The call answers `402` with the x402 terms for exactly that amount in USDC
   (1 USD = 1 USDC), paid to the donation's own receiving address
   (`DMD_USDC_BASE_INFERENCE_ADDRESS`, DebitMyData's "AgenticInference Pay" wallet), not to
   the DID payment address. Without that address, donations are not offered.
2. With `PAYMENT-SIGNATURE`, the signed authorization is checked against those terms (and
   must expire within the donation's 15-minute window), then stored with a new `purchases`
   row and its payment intent before anything is sent out. Each payment intent belongs to
   exactly one registration or one purchase.
3. From there it is a DID payment in every respect: facilitator verify and settle, the
   transfer checked on chain, confirmations, crediting once, the payment worker and reorg
   handling. The purchase follows its payment (`awaiting_payment` → `paid`, or `failed` when
   nothing was received).
4. Once paid, the donation is completed with a receipt: a W3C credential
   (`DebitMyDataDonationReceipt`, signed with the issuer key like the DID credential) whose
   subject is the donor's DID, or the paying wallet as `did:pkh`. The answer is `201` with
   the receipt, or `202` with `status_url` (`GET /v1/donations/{purchase_id}`) while
   confirming.

An authorization already received returns the same donation (`200`) and is never settled
again; one used for another amount is refused. A call without `amount_usd` (Coinbase's
validator posts an empty body first) answers `402` with the terms of an example amount and
can never be paid. A payment refused by the facilitator closes its donation at once; if a
completed donation's payment is later reorganized out of the chain, an alert is recorded in
the audit trail. Through MCP, `dmd_catalog` lists the donation and `dmd_purchase` pays it.

## Controller proof

The quote names the DID's controller: an Ed25519 public key, an EVM wallet, or both. Each
one named must prove control before the DID is issued.

* **Ed25519 key:** `ed25519:` followed by a Multikey `z6Mk...`, 64 hex characters or
  base64. Keys are checked at quote time, including that they are valid curve points of
  large order, and stored in Multikey form. The agent signs the exact UTF-8 bytes of the
  challenge.
* **EVM wallet (ID-06):** `0x...` (EIP-55 checksummed or all one case; a wrong checksum and
  the zero address are refused) or a CAIP-10 account `eip155:<chain id>:0x...`, on one of
  the networks of the approved EVM rails (Base, `eip155:8453`; Base Sepolia on staging);
  `controller_network` picks one, by default the x402 rail's. It is stored checksummed,
  with its network. The wallet signs the challenge the way wallets sign text (EIP-191,
  `personal_sign` / `signMessage`) and the agent sends the signature as hex:
  * an ordinary wallet (EOA): the address recovered from the signature must be the
    wallet's. Checked by the gateway itself, without asking the chain;
  * a smart-contract wallet such as Coinbase Smart Wallet (ERC-1271): the wallet contract
    says whether the signature is valid;
  * a smart wallet that is not deployed yet (ERC-6492): the signature carries the call that
    would deploy it, and the check simulates that deployment.

  Smart wallets are checked with one read-only `eth_call` on the wallet's network (the
  x402 rail's RPC), which runs ERC-6492's reference validator without deploying anything
  (`contracts/UniversalSigValidator.sol`, CC0, compiled into
  `identity/erc6492.py`). If that network cannot be asked right now, the answer is `503`
  with `Retry-After` and the challenge stays open; on a network without an RPC the answer
  is `422 smart_wallet_not_supported`, also leaving the challenge open.

Challenges and proofs work the same for both:

* A challenge is for one controller: by default the next one still to prove, or the one
  named in `controller_type` (`ed25519` or `eip155`). It is a short text containing the
  gateway, the registration, the controller, a random nonce and its validity window
  (5 minutes).
* A challenge can be answered once. A wrong signature uses it up, so it cannot be replayed
  or guessed; a malformed one (not base64url/base64/hex for a key, not hex for a wallet)
  does not.
* At most 5 unanswered challenges can be open per registration.
* Every answer is stored in `controller_proofs` with how it was checked
  (`ed25519_signature`, `eip191_signature`, `erc1271_signature`, `erc6492_signature`),
  which is append-only, and audited.
* `GET /v1/dids/registrations/{id}` lists the controllers in `still_to_prove`.

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
| `dmd_catalog`, `dmd_get_purchase_quote` | the offers and donations, and their enabled payment options |
| `dmd_purchase` | for `did_registration`, the same as creating a quote; for `inference_donation`, `POST /v1/donations/inference` (the x402 payment in `x402_payment` or `_meta["x402/payment"]`) |
| `dmd_marketplace_provider_requirements` | states that provider onboarding is not open yet |

Failed calls return the REST error body (`http_status`, `error.code`, details) as a tool
error. Requests whose `Host` or `Origin` is not the gateway's own address are refused
(DNS-rebinding protection); `DMD_GW_MCP_ALLOWED_HOSTS` adds host names when needed. Agents
sign challenges themselves: no tool takes or returns a private key.

## Identity issuance rule

An identity is issued only when **all** of these hold:

* payment status is `settled`;
* controller proof is `verified` for each controller the registration names (its key and/or
  its wallet);
* policy checks passed: the payment rail is still enabled (a killed rail also stops
  issuance), the settled outputs are credited to this payment, and the registration is not
  held for review. A controller key may hold at most 5 open quotes at a time (quotes refuse
  more); review thresholds, anomaly detection and compliance screening are still to come
  (POL-*, DEC-10, DEC-11);
* the registration intent is valid (not expired, rejected or cancelled) and has not been
  used before.

Issuance is idempotent: repeating `POST /v1/dids/registrations` returns the identity that
was already issued; one registration intent produces at most one identity
(`identities.registration_intent_id` is unique), each identity is bound to the proof that
verified its controller (`identities.controller_proof_id`: the key's, or the wallet's when
there is no key; with both, the wallet's proof is in the `identity.issued` audit event),
and one payment output can be
credited to at most one intent (`credited_outputs` primary key). If a payment behind an
issued identity is later reorganized away, the worker records the alert
`alert.issued_identity_payment_reorged` for an operator.

## Identifiers, DID documents and credentials

* **DID (DEC-05):** `did:web:<gateway host>:agents:<id>`. `did:web` is an existing DID
  method, so standard resolvers fetch `https://<gateway host>/agents/<id>/did.json`, which
  lists the controller key (`Multikey`) and/or the wallet
  (`EcdsaSecp256k1RecoveryMethod2020` with its `blockchainAccountId`, as `did:pkh` does)
  for authentication and assertions. A deactivated identity answers 410.
* **Issuer:** `did:web:<gateway host>`, whose document at `/.well-known/did.json` lists the
  Ed25519 key that signs credentials.
* **Credential (DEC-06):** a W3C Verifiable Credential (data model 2.0) of type
  `DebitMyDataRegistrationCredential`, signed with a Data Integrity proof using the
  `eddsa-jcs-2022` cryptosuite (RFC 8785 canonical JSON, SHA-256, Ed25519). It is also the
  signed registration receipt: subject DID, display name, participant type, the
  controllers (key and/or wallet), how and when each was verified, registration and payment intent, rail, asset, network, amount,
  settled outputs, commercial price, terms version and entitlements.
  `scripts/demo_agent.py verify-credential` checks one using only public information.
* **Entitlements:** `dmd_did_holder` is granted with the identity; product and marketplace
  eligibility come later (ID-11).
* **Issuer key custody (ID-15):** development and staging read the key from a file only its
  owner can read (`DMD_GW_ISSUER_KEY_PATH`, created with `dmd-gateway-issuer-key`).
  Production refuses a key file. There the key belongs to the credential signer
  (`dmd-gateway-signer`, `src/dmd_gateway/signer`): its own system user, no network access,
  reached by the API through a local socket that only the gateway's user can open. Before
  signing, it checks the facts in the database itself through a read-only role: a
  registration credential only for a settled payment with a verified controller proof and no
  credential yet; a donation receipt only for a settled donation without one. The API checks
  every returned signature against the published key (`DMD_GW_ISSUER_PUBLIC_KEY`), and answers
  503 `issuance_unavailable` (nothing issued, the payment kept) while the signer is down. A
  compromised API host can therefore neither copy the key nor obtain a credential for
  something unpaid. The key is backed up encrypted with the nightly backup
  ([production.md](production.md)); rotating it means publishing the new key while keeping
  the old one verifiable, a procedure still to write.

## Policy pages

Every quote names the terms version it is made under and links to the policies (the
directive's terms, privacy, refund, payment and DID policies, plus security). The gateway
serves these pages itself, so the links always work and match the server that made the
quote:

| Page | Address | Linked as |
| --- | --- | --- |
| Terms of service | `/policies/terms` | `terms` |
| Privacy policy | `/policies/privacy` | `privacy` |
| Security | `/policies/security` | `security` |
| Payment methods | `/policies/payment-methods` | `payment_policy` |
| Refund policy | `/policies/refunds` | `refund_policy` |
| DID policy | `/policies/did` | `did_policy` |

* The texts are Markdown files in `src/dmd_gateway/policies`, so changes are reviewed like
  code. Each has a version (its date) and a status: `draft` until DebitMyData approves it,
  shown on the page.
* Placeholders fill in what differs between servers: the gateway's address, the DID host,
  the issuer DID, the quote and challenge lifetimes, and the table of approved payment
  methods, which is built from the payment route registry.
* `/policies/<name>.md` returns the text as Markdown for agents, and `GET /v1/policies` lists
  each page with its version, status and SHA-256.
* `terms_version` in the route registry is the version of the terms page (the tests keep
  them equal), and every registration and credential records it. Earlier versions stay in
  the repository's history.
* In `policy_urls`, a path is a page of this gateway and becomes a full URL in quotes and
  discovery; any other link must be an https URL.
* The pages run no scripts: their Content-Security-Policy only allows their own style and
  the gateway's icon, and HTML inside a text is shown, never run. They are sent with
  `Cache-Control: no-transform`, so Cloudflare serves them unchanged; otherwise its email
  obfuscation would replace each address with "[email protected]" and a decoding script
  that the policy blocks.

## Identifier wording

The product is "DebitMyData Agentic DID Registration", and public descriptions use the
wording set in the directive. Identities are standard `did:web` identifiers. Do not claim a
`did:dmd` method until its method specification, resolution rules and governance are
published.
