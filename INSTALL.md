# Installing the DebitMyData Agent Gateway

How to run the gateway on your computer for development, test it, configure its payment
rails, and where the server guides are. The API itself is described in
[docs/api.md](docs/api.md).

## Running it on your computer

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

Request a DID-LLM registration quote (the directive's example request, with an example
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

DID-LLM issuance needs a credential signing key (`DMD_GW_ISSUER_KEY_PATH`, created with
`dmd-gateway-issuer-key secrets/issuer-key.pem`). Without it, everything else works and the
issuance endpoint answers `503`. Run commands from the repository root (the route registry path is relative to it).

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

CI (`.github/workflows/ci.yml`) runs lint, shellcheck on every server script, a syntax check
of the pages' scripts and the full suite, database tests included, on every push and pull
request. It also audits the libraries a new install gets with `pip-audit`, on every push and
every Monday, so a security fix published for one of them is seen within the week
([docs/production.md, section 8](docs/production.md#8-updates)).

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

Rails on test networks (DebitMyData regtest/testnets, Base Sepolia) are refused in production.

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
once safely. To try the whole flow on a private chain, from quote to DID-LLM, follow
[docs/regtest-demo.md](docs/regtest-demo.md).

## Servers

A server runs the API, the payment worker, the credential signer, the monitor and the nightly
backup, behind nginx and Cloudflare. [deploy/staging/](deploy/staging/) and
[deploy/production/](deploy/production/) hold their setup scripts, nginx sites and systemd
units; [docs/staging.md](docs/staging.md) and [docs/production.md](docs/production.md) walk
through a staging server (a private regtest chain, test payments) and a production server,
[docs/monitoring.md](docs/monitoring.md) the alerts, and [docs/emergencies.md](docs/emergencies.md)
what to do when something goes wrong. Each payment option has its own guide:
[card-payments.md](docs/card-payments.md), [paypal-payments.md](docs/paypal-payments.md), and
the regtest walkthrough for DMD, [regtest-demo.md](docs/regtest-demo.md).

## Project layout

```
config/payment_routes.yaml   payment rails, services, commercial price (no secrets)
config/payment_routes.regtest.yaml   the same, on test networks (DebitMyData regtest, Base Sepolia)
deploy/staging/              staging server setup (nginx, systemd services, Cloudflare-only firewall)
deploy/production/           production server setup, signer and backup units, nightly backup
server.json                  the MCP server's listing in the official MCP Registry
migrations/                  Alembic migrations (hand-reviewed)
src/dmd_gateway/
  main.py                    application factory
  mcp_server.py              MCP tools at /agenticsuperhighway, calling the REST API in-process
  worker.py                  payment worker (dmd-gateway-worker), also refreshes the DMD price
  monitoring/                health checks, email/Discord alerts, daily reconciliation report
                             (dmd-gateway-monitor)
  identity/                  controller keys, did:web documents, issuer key, signed credentials
  signer/                    the production credential signer (dmd-gateway-signer) and its client
  handshake.py               the discovery runbook's Day 5 check (dmd-gateway-handshake)
  oauth/                     OAuth: the token-signing key (dmd-gateway-oauth-key), the agents'
                             signed requests, access tokens, the apps (Client ID Metadata
                             Documents), sign-in grants
  runtime.py                 registry, adapters and price sources shared by API and worker
  settings.py                environment configuration
  observability.py           JSON logging, correlation IDs
  audit.py                   append-only audit helper
  api/                       HTTP routes
  adapters/                  payment adapters (native DMD, x402 USDC), restricted RPC clients,
                             x402 facilitator client
  pricing/                   price-source rules and the DMD mining-cost price
  services/                  quotes, settlement, payment references, controller proofs,
                             registrations, x402 payments, donations, staging test tools
  db/                        SQLAlchemy models and session handling
  domain/                    money, ids, payment states, settlement rules, issuance rule, registry
scripts/demo_agent.py        agent side for demos: key, signature, credential check, MCP client
scripts/did-owner-key.py     owner key for a DID-LLM without a wallet (docs/owner-key.md)
scripts/load_test.py         load test: requests per second and response times
scripts/backup-restore-test.sh  the backups' restore test, on your own computer (production.md, 4e)
scripts/firewall-check.sh    the server's firewall, checked from outside (emergencies.md, part 8)
tests/
docs/                        architecture, roadmap, operator checklists, emergencies
```

Before connecting the gateway to any DebitMyData node, verify that node's RPC isolation with
[docs/sec-04-rpc-isolation.md](docs/sec-04-rpc-isolation.md).

## Ground rules

These apply to every change in this repository:

- Do not modify DebitMyData consensus code to support gateway features.
- Never expose DebitMyData Core RPC publicly or forward arbitrary RPC methods.
- Never give callers private keys, seed phrases, wallet unlock or unrestricted signing.
- No treasury or spending keys on gateway hosts; payment tracking is watch-only.
- Never accept an asset by ticker symbol; match chain ID + canonical contract/identifier.
- Never use binary floating point for money or token amounts.
- Never issue an identity unless payment is `settled`, controller proof is `verified`,
  policy checks passed and the registration intent is valid and unused.
- Never put secrets in source, tickets, logs or chat. `.env` files are git-ignored.
- Public documentation describes only rails that are actually enabled and verified.

## Database notes

`audit_events`, `payment_events`, `credited_outputs`, `controller_proofs` and
`payment_references` are append-only: database triggers reject `UPDATE`, `DELETE` and `TRUNCATE`. `credited_outputs` has a primary key on
(network, transaction, output index), so a payment output can be credited to only one
intent. In staging and production, run migrations as a separate owner role and let the
application connect as a role that has only the privileges it needs.
