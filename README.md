# DebitMyData Agent Gateway

The front door of DebitMyData for AI agents, apps, people and organizations. Through one gateway,
an autonomous agent can **discover DebitMyData, get a quote, pay with an approved payment option,
prove that it controls its key or wallet, and receive a DebitMyData DID-LLM**: a verifiable
identity (a W3C `did:web` DID) with a credential signed by DebitMyData, paid once, with no account
and no password.

- **For people**: [id.debitmydata.io](https://id.debitmydata.io), "Get a DID-LLM", also an app for
  phones.
- **For agents and developers**: [api.debitmydata.io](https://api.debitmydata.io), with its REST
  API, its MCP server (the Agentic Superhighway, at `/agenticsuperhighway`) and its guide for
  agents, `/llms.txt`.

The gateway sits *around* the DebitMyData Proof-of-Work chain. It does not change DebitMyData
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
        ├── Native DMD adapter ──► private DebitMyData nodes (watch-only)
        ├── x402 / USDC Base adapter ──► facilitator
        ├── Card adapter ──► Stripe Checkout (payment page hosted by Stripe)
        ├── PayPal adapter ──► PayPal Checkout (payment page hosted by PayPal)
        └── future approved rails (disabled until reviewed)
        │
        ▼
Settlement & finality ──► DID-LLM + receipt + credential + entitlements ──► append-only audit
```

## What it does

**Identity for agents.**
- Issues **DID-LLMs**: `did:web` identities with a W3C Verifiable Credential 2.0
  (`eddsa-jcs-2022`), signed by DebitMyData's issuer key and checked again on every use.
- Lets their owners **prove control** with an Ed25519 key or an EVM wallet (ordinary wallets, and
  smart wallets through ERC-1271 and ERC-6492), **change owners** and **revoke** a DID-LLM, from the
  API, the MCP server or the "Manage your DID-LLM" page.
- **Verifies agents**: any service can check a DID-LLM it meets, and ask it for a fresh proof of
  control.
- A public **directory** where owners can list their DID-LLM with a signed profile.

**Payments, at a fixed 25.00 USD price.**
- **DMD**, the DebitMyData coin, tracked by the gateway's own watch-only node, with confirmations
  and reorganization handling; priced by DebitMyData's reference price (the cost of mining DMD,
  with a minimum rate).
- **USDC on Base through x402**, the HTTP payment standard for agents, settled through Coinbase's
  facilitator and checked on chain.
- **Card (Stripe) and PayPal**, on their own hosted payment pages, for people.
- Every payment option is off until it is configured and approved, and a DID-LLM is issued only
  once its payment is settled.

**Made for agents to find and use.**
- An **MCP server** whose tools do everything the REST API does, with an MCP Server Card and a
  listing for the official MCP Registry (`server.json`).
- **A2A** (the Agent2Agent protocol), `/llms.txt`, an installable Agent Skill, and x402 Bazaar
  descriptions.
- **Sign-in with a DID-LLM** (OAuth 2.1): agents get access tokens with their own key, and people
  sign in to apps such as MCP clients with their DID-LLM.

**Products and donations.** Add-ons bought for a DID-LLM (BottyBotty, in three tiers) with signed
receipts, and inference donations from agents in USDC.

**Operations.** Monitoring and alerts by email and Discord, a daily reconciliation report, an
encrypted nightly backup, and an append-only audit trail.

## Security by design

- No treasury or spending keys on the gateway's servers: payment tracking is watch-only.
- The issuer key is held only by a separate signer service, with no network access, which checks
  the records before it signs anything.
- Node RPC is never public, and callers never get private keys, seed phrases or signing.
- Money is never computed in floating point, and an asset is matched by its chain and contract,
  never by its ticker.
- Audit and payment records are append-only: the database refuses to change or delete them.

To report a vulnerability, see [SECURITY.md](SECURITY.md); please never use a public issue.

**Naming.** DMD is the ticker of the DebitMyData coin, the native coin of the DebitMyData
Proof-of-Work chain (addresses start with `dm1`). It is not a token on Ethereum or Base, and it is
unrelated to other tokens that use the same ticker.

## Status

Version 0.55.23, in production at api.debitmydata.io and id.debitmydata.io, with a staging server
on a private test chain at [staging-api.debitmydata.io](https://staging-api.debitmydata.io/docs)
where anyone can try the whole registration. Every capability and its state is listed in
[docs/status.md](docs/status.md); the plan and decisions are in [docs/roadmap.md](docs/roadmap.md).

## Documentation

| | |
| --- | --- |
| [INSTALL.md](INSTALL.md) | Running the gateway, its tests, its configuration and its servers |
| [docs/api.md](docs/api.md) | The registration flow: the REST API and the MCP tools |
| [docs/integrations.md](docs/integrations.md) | Connecting a product: sign-in with a DID-LLM, checking agents, owner changes |
| [docs/architecture.md](docs/architecture.md) | How it is built, and the rules behind each step |
| [docs/directory.md](docs/directory.md), [docs/owner-key.md](docs/owner-key.md) | The directory, and owning a DID-LLM with a key |
| [docs/status.md](docs/status.md), [docs/roadmap.md](docs/roadmap.md) | What is done, what is next |
