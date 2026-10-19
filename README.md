# DebitMyData Agent Gateway

> **Status: in development.** Nothing accepts real payments yet and every payment rail is
> switched off by default. The full flow runs on a private regtest chain. See [Status](#status).

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

This is an early development release (`0.4.0`). The whole registration flow (quote, native
DMD payment, controller proof, DID and signed credential) works end to end on a private
regtest chain; nothing accepts real payments yet:

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
| Controller proof: one-time challenges signed with the agent's Ed25519 key | done |
| DID issuance (`did:web`), signed registration credential (W3C VC 2.0, `eddsa-jcs-2022`), `dmd_did_holder` entitlement | done (regtest; production signer still needed) |
| MCP (Agentic Superhighway), x402 / USDC, wallet-control proofs, controller rotation and revocation | next |

Every payment rail is **off** by default. The native DMD rail can be switched on for a
private regtest chain (see [docs/regtest-demo.md](docs/regtest-demo.md)); on mainnet it still
needs an approved DMD/USD price source. See [docs/roadmap.md](docs/roadmap.md) for the task
list and decisions.

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
| Pay, then follow the payment | `GET /v1/payments/{payment_intent_id}` |
| Get a one-time message to sign with the controller key | `POST /v1/dids/registrations/{registration_intent_id}/controller-challenges` |
| Send the signature | `POST /v1/dids/registrations/{registration_intent_id}/controller-proofs` |
| See what is still missing | `GET /v1/dids/registrations/{registration_intent_id}` |
| Receive the DID, signed credential and entitlements | `POST /v1/dids/registrations` |
| Look them up | `GET /v1/dids/{identity_id}`, `GET /v1/credentials/{credential_id}`, `GET /v1/entitlements/{identity_id}` |
| Resolve the DID and the issuer key | `GET /agents/{id}/did.json`, `GET /.well-known/did.json` |

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
7. its payment adapter is implemented.

If a rail is switched on while 3–7 are not met, the gateway **refuses to start** instead of
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
config/payment_routes.regtest.yaml   the same, with the DMD rail on a private regtest chain
migrations/                  Alembic migrations (hand-reviewed)
src/dmd_gateway/
  main.py                    application factory
  worker.py                  payment worker (dmd-gateway-worker)
  identity/                  controller keys, did:web documents, issuer key, signed credentials
  runtime.py                 registry, adapters and price sources shared by API and worker
  settings.py                environment configuration
  observability.py           JSON logging, correlation IDs
  audit.py                   append-only audit helper
  api/                       HTTP routes
  adapters/                  payment adapters (native DMD) and the restricted DMD RPC client
  pricing/                   price-source rules (approved sources only)
  services/                  quotes, payment settlement, controller proofs, registrations, idempotency
  db/                        SQLAlchemy models and session handling
  domain/                    money, ids, payment states, settlement rules, issuance rule, registry
scripts/demo_agent.py        agent side for demos: key, challenge signature, credential check
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

`audit_events`, `payment_events`, `credited_outputs` and `controller_proofs` are
append-only: database triggers reject `UPDATE`, `DELETE` and `TRUNCATE`. `credited_outputs` has a primary key on
(network, transaction, output index), so a payment output can be credited to only one
intent. In staging and production, run migrations as a separate owner role and let the
application connect as a role that has only the privileges it needs.
