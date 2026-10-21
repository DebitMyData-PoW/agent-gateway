# Roadmap

Work is tracked by task ID. Keep each change small and tied to one or a few IDs.

**First production release:** native DMD + DID issuance + REST/MCP + x402 USDC on Base.
Every other rail stays disabled until it passes its own review.

Legend: `[x]` done · `[ ]` open. Items marked *(schema)* have their database constraint in
place; the application logic comes with the related phase.

## Phase 0 – Setup and inventory

- [x] P0-01 Dedicated Agent Gateway repository
- [ ] P0-02 Tag/back up the current DMD Core production release
- [ ] P0-03 Inventory nodes, Electrum servers, explorer, wallets, pool, DNS, TLS, hosting (kept outside this public repository)
- [ ] P0-04 Decide which hosts may reach private DMD RPC; deny all others
- [x] P0-05 Staging environment (`deploy/staging`, [staging.md](staging.md)), with staging-only test tools so the flow can be tried from its docs page; production comes after the security review
- [ ] P0-06 Owners for DNS/TLS, DevOps, database, treasury, policy approval, incident response
- [ ] P0-08 Secrets inventory (never in tickets, chat, source or docs)
- [ ] P0-09 Branch/release strategy and rollback procedure

## Phase 1 – DMD infrastructure hardening

- [ ] SEC-01 All public DMD nodes on the intended Core release and chain
- [ ] SEC-02 Chain-anchor / fork protections confirmed
- [ ] SEC-03 Review upstream Core security changes relevant to the DMD base
- [ ] SEC-04 RPC bound to loopback/private network only (operator checklist: [sec-04-rpc-isolation.md](sec-04-rpc-isolation.md))
- [ ] SEC-05 Dedicated RPC credentials for the gateway adapter
- [x] SEC-06 Method-restricted RPC allowlist in the gateway's RPC client (a node-side proxy is still recommended)
- [x] SEC-07 Explicit allowed-method list; reject unlock, key export, admin, mining, shutdown
- [ ] SEC-08 Payment receive/tracking wallet separate from treasury
- [x] SEC-09 Watch-only / descriptor-based payment tracking (the gateway refuses a wallet that can spend)
- [ ] SEC-10 Firewall verified from an external host
- [ ] SEC-11 Secondary verification node with tip comparison
- [ ] SEC-12 Node health and sync checks
- [ ] SEC-13 Reorg monitoring and alert thresholds
- [ ] SEC-14 Backup and restore test (descriptors, configuration, database)
- [ ] SEC-15 Emergency RPC isolation and DMD rail kill-switch runbook

## Phase 2 – Gateway foundation

- [x] GW-01 Service skeleton (Python/FastAPI)
- [x] GW-02 Environment-based configuration (development/staging/production)
- [x] GW-03 Structured configuration for domains, route registry, feature flags
- [x] GW-04 Database schema for intents, payments, identities, proofs, credentials, entitlements, policy versions, audit
- [x] GW-05 Append-only audit semantics (database triggers)
- [x] GW-06 Idempotency for mutating endpoints (`Idempotency-Key` header on quotes; DID issuance is idempotent by registration)
- [x] GW-07 Request correlation IDs and safe structured logs
- [x] GW-08 Health/readiness endpoints that expose no secrets
- [ ] GW-09 Authentication boundaries for public, internal and operator endpoints
- [x] GW-10 OpenAPI documentation for public routes
- [x] GW-11 Background worker for polling and finality (`dmd-gateway-worker`); full reconciliation is DMD-13
- [x] GW-12 Per-rail feature flags and kill switches (environment-based; runtime toggle later with OPS-09)

## Phase 3 – Route registry and quote engine

- [x] QUOTE-01 Payment-options route registry
- [x] QUOTE-02 `dmd_pow_native` configured (disabled on mainnet pending DEC-01; enabled on regtest for demos)
- [x] QUOTE-03 `x402_usdc_base` configured (disabled pending X402-03, DEC-08)
- [x] QUOTE-04 Future EVM/Solana/wrapped-DMD rails unapproved and disabled
- [x] QUOTE-05 `POST /v1/dids/registration-quotes`
- [x] QUOTE-06 Globally unique intent IDs
- [x] QUOTE-07 Fixed 25.00 USD commercial price in every quote
- [x] QUOTE-08 Asset/network/protocol/recipient/finality/expiry/policy version in every quote
- [x] QUOTE-09 DMD/USD price source behind an abstraction (the actual source waits on DEC-01)
- [x] QUOTE-10 Quote TTL (`DMD_GW_QUOTE_TTL_SECONDS`); payments first seen after expiry are never credited
- [x] QUOTE-11 Store price inputs for audit/reconciliation
- [x] QUOTE-12 Stale-price/price-outage (quote refused); underpayment, overpayment and late payment recorded as refund due (DEC-04)
- [x] QUOTE-13 Exact decimal arithmetic with unit tests

## Phase 4 – Native DMD payment adapter

- [x] DMD-01 `DMDNativePaymentAdapter` behind the common interface
- [x] DMD-02 Unique payment address per intent
- [x] DMD-03 Destination stored with the intent before the quote is returned
- [x] DMD-04 Detect arrival through the restricted node interface
- [x] DMD-05 Independently verify txid, outputs, destination, amount, chain
- [x] DMD-06 Confirmation tracking through the normalized states
- [x] DMD-07 Final DMD confirmation policy: 6 confirmations (DEC-02)
- [x] DMD-08 Reorg detection and handling
- [x] DMD-09 Double-spend / conflict / replacement detection
- [x] DMD-10 Deterministic underpaid/overpaid handling
- [x] DMD-11 One output credited to at most one intent
- [x] DMD-12 Payer-submitted reference treated as a hint only (`POST /v1/payments/{id}/references`, MCP `dmd_submit_payment_reference`)
- [ ] DMD-13 Periodic reconciliation against the chain
- [ ] DMD-14 Node failover / second verification node
- [x] DMD-15 Address reuse prevention and treasury separation (watch-only gateway wallet)

## Phase 5 – Controller proof, identity and credentials

- [x] ID-01 Identity record with lifecycle states (active/suspended/revoked; the transitions come with ID-13)
- [x] ID-02 Public identifier: `did:web:<gateway host>:agents:<id>` (DEC-05); no `did:dmd` claim
- [x] ID-03 One-time challenges with a short expiry (5 minutes), limited per registration
- [x] ID-04 Controller proof per key family: Ed25519 keys first
- [ ] ID-05 Native DMD wallet proof supported by official DMD wallets (DEC-07)
- [ ] ID-06 EVM controller proof, with the x402/EVM rail
- [ ] ID-07 Solana controller proof, only when that rail is enabled
- [x] ID-08 Verified proof bound to the registration and the identity
- [x] ID-09 Identity issued only after settlement, verified proof and policy checks; idempotent
- [x] ID-10 Signed registration credential/receipt (W3C VC 2.0, `eddsa-jcs-2022`, DEC-06)
- [ ] ID-11 Entitlements: `dmd_did_holder` done; product and marketplace eligibility open
- [ ] ID-12 Controller rotation with proof and audit history
- [ ] ID-13 Suspension/revocation/recovery without deleting history
- [ ] ID-14 Transfer/new-registration policy for material controller changes
- [ ] ID-15 Issuer key custody: key file for development/staging (refused in production); separate production signer and rotation procedure open

## Phase 6 – MCP server

- [x] MCP server at `/mcp` (official MCP SDK, Streamable HTTP, stateless, DNS-rebinding protection)
- [x] All twelve directive tools: `dmd_discover`, `dmd_get_did_registration_offer`,
  `dmd_list_supported_payment_options`, `dmd_create_did_payment_quote`,
  `dmd_submit_payment_reference`, `dmd_get_payment_status`, `dmd_prove_wallet_control`,
  `dmd_register_did`, `dmd_catalog`, `dmd_get_purchase_quote`, `dmd_purchase`,
  `dmd_marketplace_provider_requirements`, plus `dmd_get_registration_status`; all call the REST API in-process
- [x] Public MCP address on staging: `https://staging-api.debitmydata.io/mcp`
- [ ] Public MCP address in production: `https://api.debitmydata.io/mcp`
- [ ] Marketplace provider requirements: publish them, then return them from the tool
- [ ] Integration with the Agentic Superhighway MCP, if its owners share it

## Phase 7 – x402 / USDC on Base

- [ ] X402-01 … X402-11 Re-verify current x402/Bazaar specifications, adapter, canonical
  USDC contract, merchant address from configuration, HTTP 402 flow, settlement
  verification, idempotent fulfilment, Bazaar metadata for enabled routes only, low-value
  production validation, independent kill switch

## Phases 8–13

Additional rails (RAIL-*), abuse prevention and policy (POL-*), monitoring and operations
(OPS-*), developer documentation (DOC-*), testing and security validation (TEST-*),
deployment and cutover (DEP-*).

## Decisions

Decisions marked done were made by the development team from the directive's examples
and can be changed later.

- [ ] DEC-01 DMD/USD price source(s), fallback, maximum age, deviation tolerance
- [x] DEC-02 Native DMD confirmation requirement: 6 (from the directive's example)
- [x] DEC-03 Quote lifetime: 15 minutes (from the directive's example)
- [x] DEC-04 Underpayment: top-up allowed until expiry, then refund; overpayment: accepted, excess refunded; late payment: refunded, never credited
- [x] DEC-05 Public identity syntax: `did:web` hosted by the gateway; `did:dmd` only once a method specification is published
- [x] DEC-06 Credential format and signature: W3C VC 2.0 with Data Integrity `eddsa-jcs-2022` (Ed25519); production key custody stays open (ID-15)
- [ ] DEC-07 Controller-proof method supported by official DMD wallets
- [ ] DEC-08 Base/USDC receiving account and x402 facilitator
- [ ] DEC-09 Policy URLs and version owners
- [ ] DEC-10 Compliance/KYT requirements and provider
- [ ] DEC-11 Registration quotas and review thresholds
- [ ] DEC-12 Refund signing/approval thresholds
- [ ] DEC-13 Hosting topology and secret management
- [ ] DEC-14 Database backup and retention policy
- [ ] DEC-15 Which EVM/Solana assets enter the next phase
