# DebitMyData Agent Gateway

> **Status: in development.** Nothing accepts real payments yet and every payment rail is
> switched off by default. The full flow runs on test networks (a private DMD regtest chain,
> and Base Sepolia for USDC through x402). See [Status](#status).

The Agent Gateway is being built as the public integration surface that will let autonomous
agents, bots, digital twins, people and organizations **discover DebitMyData, get a quote,
pay through an approved rail, prove control of their wallet or key, and receive a
DebitMyData DID, signed credential and entitlements**.

The gateway sits *around* the DebitMyData Proof-of-Work chain. It does not change DMD
consensus, and it never exposes node RPC, private keys or treasury controls to callers.

```
Agent wallet (Coinbase / AWS / DMD / other approved)
        │
        ▼
Discovery: REST / MCP / x402 Bazaar
        │
        ▼
Agent Gateway ── quote + payment intent + policy
        │
        ├── Native DMD adapter ──► private DMD nodes (watch-only)
        ├── x402 / USDC Base adapter ──► facilitator
        └── future approved rails (disabled until reviewed)
        │
        ▼
Settlement & finality ──► DID + receipt + credential + entitlements ──► append-only audit
```

## Status

This is an early development release (`0.9.0`). The whole registration flow (quote,
payment, controller proof, DID and signed credential) works end to end on test networks,
through REST and through MCP, paid either in native DMD (private regtest chain) or in USDC
through x402 (Base Sepolia); nothing accepts real payments yet:

| Area | State |
| --- | --- |
| FastAPI service, settings, structured JSON logs, request correlation IDs | done |
| `/healthz`, `/readyz`, `/v1/discovery`, `/v1/payment-options`, OpenAPI at `/docs` | done |
| Payment route registry with per-rail enable flags and kill switches | done |
| Normalized payment state machine and the DID-issuance rule | done |
| Exact decimal / base-unit money arithmetic | done |
| PostgreSQL schema + migration, append-only audit and payment ledgers | done |
| Quote endpoint (`POST /v1/dids/registration-quotes`), price-source rules, `Idempotency-Key` retries | done (answers only for enabled rails) |
| Native DMD payment adapter: per-quote address, independent verification, confirmations, reorg and double-spend handling | done (regtest) |
| Payment worker (`dmd-gateway-worker`) and payment status (`GET /v1/payments/{id}`) | done |
| Controller proof: one-time challenges signed with the agent's Ed25519 key and/or its EVM wallet (ordinary wallets, and smart wallets through ERC-1271 and ERC-6492) | done |
| DID issuance (`did:web`), signed registration credential (W3C VC 2.0, `eddsa-jcs-2022`), `dmd_did_holder` entitlement | done (regtest; production signer still needed) |
| MCP server at `/mcp` with the directive's tools (Streamable HTTP), payer transaction references | done |
| Staging server behind Cloudflare; the whole flow can be tried from its `/docs` page (staging-only test tools) | done |
| x402 / USDC on Base: HTTP 402 on the registration, facilitator verify/settle, settlement checked on chain, Bazaar description, MCP payments | done (Base Sepolia) |
| Base mainnet readiness: DebitMyData's USDC receiving address (DEC-08), Coinbase's CDP facilitator with API-key authentication, an example request that answers 402 for the Bazaar validator, `scripts/validate_bazaar.py` | done (needs DebitMyData's CDP API key and the production server) |
| Request rate limits (per address and in total, on staging's nginx), open-quote limit per controller key, load-test script | done (production limits go at the edge, see [docs/staging.md](docs/staging.md#6-request-limits-and-load-testing)) |
| DMD/USD reference price from the cost of mining DMD (the electricity a block's work takes), computed every 5 minutes with its inputs stored, `GET /v1/prices/dmd-usd` | done (staging reads the DMD difficulty from the public explorer) |
| Policy pages (terms, privacy, security, payment methods, refunds, DID policy) served at `/policies`, linked from every quote | done (drafts awaiting DebitMyData's approval) |
| Native DMD wallet proofs (DEC-07), controller rotation and revocation | next |

Every payment rail is **off** by default. The native DMD rail can be switched on for a
private regtest chain (see [docs/regtest-demo.md](docs/regtest-demo.md)); on mainnet it is
priced by the cost of mining DMD and still needs the gateway's own DMD node with the
watch-only receiving wallet. The x402 USDC rail runs on Base Sepolia on staging; on
Base mainnet it pays DebitMyData's receiving address and settles through Coinbase's CDP
facilitator, which needs DebitMyData's CDP API key (DEC-08). A staging server with the full flow on a private
regtest chain runs at `https://staging-api.debitmydata.io`: anyone can try the registration
from its [docs page](https://staging-api.debitmydata.io/docs) (see [docs/staging.md](docs/staging.md)).
See [docs/roadmap.md](docs/roadmap.md) for the task list and decisions.

## Quick start

Requirements: Python 3.12+, Docker (for PostgreSQL) or a local PostgreSQL 16.

### Windows (PowerShell)

```powershell
git clone https://github.com/DebitMyData-PoW/agent-gateway.git
cd agent-gateway
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env

docker compose up -d db
alembic upgrade head
uvicorn --factory dmd_gateway.main:create_app --reload
```

### Linux / macOS

```bash
git clone https://github.com/DebitMyData-PoW/agent-gateway.git
cd agent-gateway
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

docker compose up -d db
alembic upgrade head
uvicorn --factory dmd_gateway.main:create_app --reload
```

Then open <http://localhost:8000/docs>, or:

```bash
curl http://localhost:8000/readyz
curl http://localhost:8000/v1/discovery
curl "http://localhost:8000/v1/payment-options?service=did_registration"
```

Request a DID registration quote (the directive's example request, with an example
controller key; `python scripts/demo_agent.py new-key agent-key.pem` creates a real one):

```bash
curl -X POST http://localhost:8000/v1/dids/registration-quotes \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 8f14e45f-ceea-467a-9a3b-3c5a1d2e7b90" \
  -d '{"display_name": "ResearchClaw-01", "participant_type": "autonomous_agent",
       "controller_public_key": "ed25519:z6MktwupdmLXVVqTzCw4i46r4uGyosGXRnR3XjN4Zq7oMMsw",
       "preferred_payment_option": "x402_usdc_base"}'
```

While every payment rail is off, this answers `409` with `"code": "payment_option_unavailable"`.

## Registration flow

| Step | Endpoint |
| --- | --- |
| Discover the offer and enabled payment options | `GET /v1/discovery`, `GET /v1/payment-options` |
| Get a quote (fixed 25.00 USD, exact amount in the chosen asset) | `POST /v1/dids/registration-quotes` |
| Pay (DMD), then follow the payment | `GET /v1/payments/{payment_intent_id}` |
| Get a one-time message to sign with the controller key or wallet | `POST /v1/dids/registrations/{registration_intent_id}/controller-challenges` |
| Send the signature | `POST /v1/dids/registrations/{registration_intent_id}/controller-proofs` |
| See what is still missing | `GET /v1/dids/registrations/{registration_intent_id}` |
| Receive the DID, signed credential and entitlements (x402: pay here, see below) | `POST /v1/dids/registrations` |
| Optionally report the payment transaction (a hint only) | `POST /v1/payments/{payment_intent_id}/references` |
| Look them up | `GET /v1/dids/{identity_id}`, `GET /v1/credentials/{credential_id}`, `GET /v1/entitlements/{identity_id}` |
| Resolve the DID and the issuer key | `GET /agents/{id}/did.json`, `GET /.well-known/did.json` |
| Read the terms, privacy, payment, refund, DID and security policies | `/policies`, `GET /v1/policies` |

The same steps are available as MCP tools at `/mcp` (Streamable HTTP): `dmd_discover`,
`dmd_get_did_registration_offer`, `dmd_list_supported_payment_options`,
`dmd_create_did_payment_quote`, `dmd_submit_payment_reference`, `dmd_get_payment_status`,
`dmd_prove_wallet_control`, `dmd_register_did`, `dmd_get_registration_status`, `dmd_catalog`,
`dmd_get_purchase_quote`, `dmd_purchase` and `dmd_marketplace_provider_requirements`. Each
tool calls the REST API above in-process, so both give the same results and error codes.

**Paying with x402 (USDC on Base).** Quote with `"preferred_payment_option":
"x402_usdc_base"`, prove the controller key, then call `POST /v1/dids/registrations`. It
answers `402` with a `PAYMENT-REQUIRED` header (x402 version 2, scheme `exact`: the quoted
amount in USDC base units, the USDC contract, the network and DebitMyData's receiving
address, plus a Bazaar discovery description). Call it again with the signed payment in the
`PAYMENT-SIGNATURE` header: the gateway checks it against the quote, the facilitator verifies
and settles it, the gateway checks the settlement on chain itself, and the DID comes back in
the same answer with a `PAYMENT-RESPONSE` header (or `202` if the confirmations take longer;
then call again without paying). Payment is asked for only once everything else is ready, so
an agent is never charged for a DID that cannot be issued. Any standard x402 client can pay
it; through MCP, `dmd_register_did` returns the payment requirements and takes the payment in
`x402_payment` or `_meta["x402/payment"]`.

DID issuance needs a credential signing key (`DMD_GW_ISSUER_KEY_PATH`, created with
`dmd-gateway-issuer-key secrets/issuer-key.pem`). Without it, everything else works and the
issuance endpoint answers `503`. See [docs/architecture.md](docs/architecture.md) for the
rules behind each step.

Run commands from the repository root (the route registry path is relative to it).

## Tests

```bash
pytest                                   # unit and API tests
ruff check . && ruff format --check .    # lint
```

Database tests run only when `TEST_DATABASE_URL` points at a **disposable** database
(they drop and recreate every table). The compose database creates one for you:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://gateway:gateway-dev-only@localhost:5432/agent_gateway_test"
pytest
```

CI runs lint plus the full suite, database tests included, on every push and pull request.

## Configuration

Gateway settings use the `DMD_GW_` prefix and can come from the environment or `.env`
(see [.env.example](.env.example)). Outside `development` the gateway refuses to start with
the development database credentials or a non-HTTPS public URL.

### Payment rails

[config/payment_routes.yaml](config/payment_routes.yaml) is the single source of truth for
how an agent may pay. A rail is enabled only when **all** of these hold:

1. its enable flag (named by `enable_env`, e.g. `DMD_RAIL_DMD_POW_NATIVE_ENABLED`) is
   `true` (`1`/`true`/`yes`/`on`);
2. its kill switch (named by `kill_env`) is unset or `0`/`false`/`no`/`off`;
3. it is `approved` in the registry;
4. canonical asset identity and confirmation/finality policy are set;
5. for non-USD assets, the price policy is complete (approved source, maximum price age,
   permitted price deviation and fallback) and its price sources are implemented;
6. its receiving destination is configured;
7. its payment adapter is implemented;
8. x402 rails only: the `exact` scheme with EIP-3009 transfers, the token's EIP-712 domain,
   a confirmation count, and their facilitator and chain RPC addresses
   (`DMD_X402_FACILITATOR_URL`, `DMD_BASE_RPC_URL`). Coinbase's CDP facilitator
   (`https://api.cdp.coinbase.com/platform/v2/x402`) also needs a usable CDP API key
   (`DMD_X402_CDP_API_KEY_ID`, `DMD_X402_CDP_API_KEY_SECRET`).

Rails on test networks (DMD regtest/testnets, Base Sepolia) are refused in production.

If a rail is switched on while 3–8 are not met, the gateway **refuses to start** instead of
running a half-configured rail. Kill switches fail safe: any unrecognised value counts as
"killed". Kill switches are read at startup, so flipping one takes effect on restart.
`/v1/payment-options` lists only enabled rails, so the API never advertises a payment method
that cannot actually be verified.

## Payment worker

Payments are tracked by a separate process that watches every open payment on the enabled
rails and moves it through the normalized statuses:

```bash
dmd-gateway-worker          # runs until stopped
dmd-gateway-worker --once   # one pass
```

It never changes a payment while the node is unreachable, and several workers can run at
once safely. To try the whole flow on a private chain, from quote to DID, follow
[docs/regtest-demo.md](docs/regtest-demo.md).

## Project layout

```
config/payment_routes.yaml   payment rails, services, commercial price (no secrets)
config/payment_routes.regtest.yaml   the same, on test networks (DMD regtest, Base Sepolia)
deploy/staging/              staging server setup (nginx, systemd services, Cloudflare-only firewall)
migrations/                  Alembic migrations (hand-reviewed)
src/dmd_gateway/
  main.py                    application factory
  mcp_server.py              MCP tools at /mcp, calling the REST API in-process
  worker.py                  payment worker (dmd-gateway-worker), also refreshes the DMD price
  identity/                  controller keys, did:web documents, issuer key, signed credentials
  runtime.py                 registry, adapters and price sources shared by API and worker
  settings.py                environment configuration
  observability.py           JSON logging, correlation IDs
  audit.py                   append-only audit helper
  api/                       HTTP routes
  adapters/                  payment adapters (native DMD, x402 USDC), restricted RPC clients,
                             x402 facilitator client
  pricing/                   price-source rules and the DMD mining-cost price
  services/                  quotes, settlement, payment references, controller proofs,
                             registrations, x402 payments, staging test tools
  db/                        SQLAlchemy models and session handling
  domain/                    money, ids, payment states, settlement rules, issuance rule, registry
scripts/demo_agent.py        agent side for demos: key, signature, credential check, MCP client
scripts/load_test.py         load test: requests per second and response times
tests/
docs/                        architecture, roadmap, operator checklists
```

Before connecting the gateway to any DMD node, verify that node's RPC isolation with
[docs/sec-04-rpc-isolation.md](docs/sec-04-rpc-isolation.md).

## Ground rules

These apply to every change in this repository:

- Do not modify DMD consensus code to support gateway features.
- Never expose DMD Core RPC publicly or forward arbitrary RPC methods.
- Never give callers private keys, seed phrases, wallet unlock or unrestricted signing.
- No treasury or spending keys on gateway hosts; payment tracking is watch-only.
- Never accept an asset by ticker symbol; match chain ID + canonical contract/identifier.
- Never use binary floating point for money or token amounts.
- Never issue an identity unless payment is `settled`, controller proof is `verified`,
  policy checks passed and the registration intent is valid and unused.
- Never put secrets in source, tickets, logs or chat. `.env` files are git-ignored.
- Public documentation describes only rails that are actually enabled and verified.

## Reporting security issues

Please report vulnerabilities privately, never in public issues. See
[SECURITY.md](SECURITY.md).

## Database notes

`audit_events`, `payment_events`, `credited_outputs`, `controller_proofs` and
`payment_references` are append-only: database triggers reject `UPDATE`, `DELETE` and `TRUNCATE`. `credited_outputs` has a primary key on
(network, transaction, output index), so a payment output can be credited to only one
intent. In staging and production, run migrations as a separate owner role and let the
application connect as a role that has only the privileges it needs.
