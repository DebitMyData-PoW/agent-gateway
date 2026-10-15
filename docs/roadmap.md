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
- [ ] P0-05 Staging and production environments
- [ ] P0-06 Owners for DNS/TLS, DevOps, database, treasury, policy approval, incident response
- [ ] P0-08 Secrets inventory (never in tickets, chat, source or docs)
- [ ] P0-09 Branch/release strategy and rollback procedure

## Phase 1 – DMD infrastructure hardening

- [ ] SEC-01 All public DMD nodes on the intended Core release and chain
- [ ] SEC-02 Chain-anchor / fork protections confirmed
- [ ] SEC-03 Review upstream Core security changes relevant to the DMD base
- [ ] SEC-04 RPC bound to loopback/private network only (operator checklist: [sec-04-rpc-isolation.md](sec-04-rpc-isolation.md))
- [ ] SEC-05 Dedicated RPC credentials for the gateway adapter
- [ ] SEC-06 Method-restricted RPC proxy or allowlist
- [ ] SEC-07 Explicit allowed-method list; reject unlock, key export, admin, mining, shutdown
- [ ] SEC-08 Payment receive/tracking wallet separate from treasury
- [ ] SEC-09 Watch-only / descriptor-based payment tracking
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
- [ ] GW-06 Idempotency-key support for mutating endpoints *(schema)*
- [x] GW-07 Request correlation IDs and safe structured logs
- [x] GW-08 Health/readiness endpoints that expose no secrets
- [ ] GW-09 Authentication boundaries for public, internal and operator endpoints
- [x] GW-10 OpenAPI documentation for public routes
- [ ] GW-11 Background worker for polling, finality and reconciliation
- [x] GW-12 Per-rail feature flags and kill switches (environment-based; runtime toggle later with OPS-09)

## Phase 3 – Route registry and quote engine

- [x] QUOTE-01 Payment-options route registry
- [x] QUOTE-02 `dmd_pow_native` configured (disabled pending DEC-01, DEC-02)
- [x] QUOTE-03 `x402_usdc_base` configured (disabled pending X402-03, DEC-08)
- [x] QUOTE-04 Future EVM/Solana/wrapped-DMD rails unapproved and disabled
- [ ] QUOTE-05 `POST /v1/dids/registration-quotes`
- [x] QUOTE-06 Globally unique intent IDs
- [ ] QUOTE-07 Fixed 25.00 USD commercial price in every quote
- [ ] QUOTE-08 Asset/network/protocol/recipient/finality/expiry/policy version in every quote
- [ ] QUOTE-09 DMD/USD price source behind an abstraction
- [ ] QUOTE-10 Quote TTL; reject expired quotes
- [ ] QUOTE-11 Store price inputs for audit/reconciliation *(schema)*
- [ ] QUOTE-12 Underpayment, overpayment, stale-price, price-outage and refund behaviour
- [x] QUOTE-13 Exact decimal arithmetic with unit tests

## Phase 4 – Native DMD payment adapter

- [ ] DMD-01 `DMDNativePaymentAdapter` behind the common interface
- [ ] DMD-02 Unique payment address per intent
- [ ] DMD-03 Destination stored with the intent before the quote is returned
- [ ] DMD-04 Detect arrival through the restricted node interface
- [ ] DMD-05 Independently verify txid, outputs, destination, amount, chain
- [ ] DMD-06 Confirmation tracking through the normalized states
- [ ] DMD-07 Final DMD confirmation policy (DEC-02)
- [ ] DMD-08 Reorg detection and handling
- [ ] DMD-09 Double-spend / conflict / replacement detection
- [ ] DMD-10 Deterministic underpaid/overpaid handling
- [ ] DMD-11 One output credited to at most one intent *(schema)*
- [ ] DMD-12 Payer-submitted reference treated as a hint only
- [ ] DMD-13 Periodic reconciliation against the chain
- [ ] DMD-14 Node failover / second verification node
- [ ] DMD-15 Address reuse prevention *(schema)* and treasury separation

## Phase 5 – Controller proof, identity and credentials

- [ ] ID-01 … ID-15 Identity schema *(schema)*, one-time challenges *(schema)*, per-wallet proof methods,
  binding, issuance after settlement, signed credentials, entitlements, rotation,
  suspension/revocation/recovery without deleting history, issuer-key custody

## Phase 6 – MCP server

- [ ] MCP-01 … MCP-16 `dmd_discover`, `dmd_get_did_registration_offer`,
  `dmd_list_supported_payment_options`, `dmd_create_did_payment_quote`,
  `dmd_submit_payment_reference`, `dmd_get_payment_status`, `dmd_prove_wallet_control`,
  `dmd_register_did`, `dmd_catalog`, `dmd_get_purchase_quote`, `dmd_purchase`,
  `dmd_marketplace_provider_requirements`, all calling the same services as REST

## Phase 7 – x402 / USDC on Base

- [ ] X402-01 … X402-11 Re-verify current x402/Bazaar specifications, adapter, canonical
  USDC contract, merchant address from configuration, HTTP 402 flow, settlement
  verification, idempotent fulfilment, Bazaar metadata for enabled routes only, low-value
  production validation, independent kill switch

## Phases 8–13

Additional rails (RAIL-*), abuse prevention and policy (POL-*), monitoring and operations
(OPS-*), developer documentation (DOC-*), testing and security validation (TEST-*),
deployment and cutover (DEP-*).

## Open decisions (required before production)

- [ ] DEC-01 DMD/USD price source(s), fallback, maximum age, deviation tolerance
- [ ] DEC-02 Native DMD confirmation/finality requirement
- [ ] DEC-03 Quote lifetime
- [ ] DEC-04 Underpayment / overpayment / late-payment / refund policy
- [ ] DEC-05 Public identity syntax; whether and when to standardize `did:dmd`
- [ ] DEC-06 Credential format, signature algorithm, issuer key custody
- [ ] DEC-07 Controller-proof method supported by official DMD wallets
- [ ] DEC-08 Base/USDC receiving account and x402 facilitator
- [ ] DEC-09 Policy URLs and version owners
- [ ] DEC-10 Compliance/KYT requirements and provider
- [ ] DEC-11 Registration quotas and review thresholds
- [ ] DEC-12 Refund signing/approval thresholds
- [ ] DEC-13 Hosting topology and secret management
- [ ] DEC-14 Database backup and retention policy
- [ ] DEC-15 Which EVM/Solana assets enter the next phase
