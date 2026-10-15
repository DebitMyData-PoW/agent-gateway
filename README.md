# DebitMyData Agent Gateway

The public integration surface that lets autonomous agents, bots, digital twins, people
and organizations **discover DebitMyData, get a quote, pay through an approved rail, prove
control of their wallet or key, and receive a DebitMyData identity (DID), signed credential
and entitlements**.

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

This is the **foundation release** (`0.1.0`). It contains everything the payment rails and
DID issuance plug into, and nothing yet accepts payments:

| Area | State |
| --- | --- |
| FastAPI service, settings, structured JSON logs, request correlation IDs | done |
| `/healthz`, `/readyz`, `/v1/discovery`, `/v1/payment-options`, OpenAPI at `/docs` | done |
| Payment route registry with per-rail enable flags and kill switches | done |
| Normalized payment state machine and the DID-issuance rule | done |
| Exact decimal / base-unit money arithmetic | done |
| PostgreSQL schema + migration, append-only audit and payment ledgers | done |
| Quote engine (`POST /v1/dids/registration-quotes`) | next |
| Native DMD adapter, controller proof, DID issuance, MCP server, x402 | later phases |

Every payment rail is **off**. See [docs/roadmap.md](docs/roadmap.md) for the task list and
the open decisions that must be made before a rail can be switched on.

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
5. for non-USD assets, the price policy is complete: approved source, maximum quote age,
   permitted price deviation and fallback;
6. its receiving destination is configured;
7. its payment adapter is implemented.

If a rail is switched on while 3–7 are not met, the gateway **refuses to start** instead of
running a half-configured rail. Kill switches fail safe: any unrecognised value counts as
"killed". Kill switches are read at startup, so flipping one takes effect on restart. `/v1/payment-options` lists only enabled rails, so the API never advertises a
payment method that cannot actually be verified.

## Project layout

```
config/payment_routes.yaml   payment rails, services, commercial price (no secrets)
migrations/                  Alembic migrations (hand-reviewed)
src/dmd_gateway/
  main.py                    application factory
  settings.py                environment configuration
  observability.py           JSON logging, correlation IDs
  audit.py                   append-only audit helper
  api/                       HTTP routes
  adapters/                  payment-adapter interface (one adapter per rail)
  db/                        SQLAlchemy models and session handling
  domain/                    money, ids, payment states, issuance rule, route registry
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

## Database notes

`audit_events`, `payment_events` and `credited_outputs` are append-only: database triggers
reject `UPDATE`, `DELETE` and `TRUNCATE`. `credited_outputs` has a primary key on
(network, transaction, output index), so a payment output can be credited to only one
intent. In staging and production, run migrations as a separate owner role and let the
application connect as a role that has only the privileges it needs.
