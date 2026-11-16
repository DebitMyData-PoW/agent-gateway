# DebitMyData Agent Gateway

> **Status: in development.** Nothing accepts real payments yet and every payment rail is
> switched off by default. The full flow runs on test networks (a private DebitMyData regtest
> chain, and Base Sepolia for USDC through x402). See [Status](#status).

The Agent Gateway is being built as the public integration surface that will let autonomous
agents, bots, digital twins, people and organizations **discover DebitMyData, get a quote,
pay through an approved rail, prove control of their wallet or key, and receive a
DebitMyData DID-LLM, signed credential and entitlements**.

The gateway sits *around* the DebitMyData Proof-of-Work chain. It does not change
DebitMyData consensus, and it never exposes node RPC, private keys or treasury controls to
callers.

**Naming.** DMD is the ticker of the DebitMyData coin, the native coin of the DebitMyData
Proof-of-Work chain (addresses start with `dm1`); it is what every DebitMyData wallet shows.
It is not a token on Ethereum or Base, and it is unrelated to other tokens that use the same
ticker. So amounts and prices are in **DMD**, the payment option is named **DebitMyData coin
(native PoW chain)**, and the project, its chain, nodes and explorer are called
**DebitMyData**.

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

## Status

This is an early development release (`0.52.1`). The whole registration flow (quote,
payment, controller proof, DID-LLM and signed credential) works end to end through REST and
through MCP. In production (api.debitmydata.io and id.debitmydata.io) DID-LLMs are paid in
DMD or by card; USDC through x402 runs on Base Sepolia on staging and waits for DebitMyData's
Coinbase CDP key in production:

| Area | State |
| --- | --- |
| FastAPI service, settings, structured JSON logs, request correlation IDs | done |
| `/healthz`, `/readyz`, `/v1/discovery`, `/v1/payment-options`, OpenAPI at `/docs/advanced` | done |
| "Get a DID-LLM" page for people at `/get-did` (production's `/docs` leads there): quote, payment, a message signed with their wallet, the DID-LLM | done (DMD; USDC through the API) |
| The debitmydata.com look (top bar, footer, colours, font) on every page for people: "Get a DID-LLM", the advanced API docs, the policies, staging's guided test | done |
| Advanced API docs that open like the guided pages: the steps as cards that follow the answers, bigger text and buttons, every endpoint below | done |
| Payment route registry with per-rail enable flags and kill switches | done |
| Normalized payment state machine and the DID-issuance rule | done |
| Exact decimal / base-unit money arithmetic | done |
| PostgreSQL schema + migration, append-only audit and payment ledgers | done |
| Quote endpoint (`POST /v1/dids/registration-quotes`), price-source rules, `Idempotency-Key` retries | done (answers only for enabled rails) |
| Native DMD payment adapter: per-quote address, independent verification, confirmations, reorg and double-spend handling | done (regtest) |
| Payment worker (`dmd-gateway-worker`) and payment status (`GET /v1/payments/{id}`) | done |
| Controller proof: one-time challenges signed with the agent's Ed25519 key and/or its EVM wallet (ordinary wallets, and smart wallets through ERC-1271 and ERC-6492) | done |
| DID-LLM issuance (`did:web`), signed registration credential (W3C VC 2.0, `eddsa-jcs-2022`), `dmd_did_holder` entitlement | done (regtest; production signer still needed) |
| MCP server, the Agentic Superhighway, at `/agenticsuperhighway` with the directive's tools (Streamable HTTP), payer transaction references | done |
| Staging server behind Cloudflare; the whole flow can be tried from its docs page, a guided test page on staging (advanced: Swagger UI step by step at `/docs/advanced`; staging-only test tools) | done |
| x402 / USDC on Base: HTTP 402 on the registration, facilitator verify/settle, settlement checked on chain, Bazaar description, MCP payments | done (Base Sepolia) |
| Base mainnet readiness: DebitMyData's USDC receiving address (DEC-08), Coinbase's CDP facilitator with API-key authentication, 402 with the real terms for x402 tools that probe the route without a quote (Coinbase's Bazaar validator), `scripts/validate_bazaar.py` | done (needs DebitMyData's CDP API key and the production server) |
| Request rate limits (per address and in total, on staging's nginx), open-quote limit per controller key, load-test script | done (production limits go at the edge, see [docs/staging.md](docs/staging.md#6-request-limits-and-load-testing)) |
| DMD/USD reference price from the cost of mining DMD (the electricity a block's work takes), computed every 5 minutes with its inputs stored, `GET /v1/prices/dmd-usd` | done (staging reads the DebitMyData difficulty from the public explorer) |
| Monitoring and alerts by email and Discord (`dmd-gateway-monitor`, every 2 minutes): API, payment worker, each payment option's node/RPC/facilitator, DMD price, DebitMyData node vs explorer, payments left the chain after issuance, refunds due, stuck payments, changed receiving addresses; daily reconciliation report that checks every payment settled in the last 30 days again on chain ([docs/monitoring.md](docs/monitoring.md)) | done (needs the email and Discord settings on each server) |
| Minimum DMD/USD rate until DMD has a market price: the price is the higher of 0.001 USD per DMD and the mining cost (a 25.00 USD DID-LLM costs 25,000 DMD), both shown on the price page | done (approved by DebitMyData on 27 October 2026) |
| Policy pages (terms, privacy, security, payment methods, refunds, DID-LLM policy) served at `/policies`, linked from every quote | done (approved, published 2026-11-02) |
| Inference donations for agents (`POST /v1/donations/inference`): an amount the agent chooses, paid in USDC through x402 to DebitMyData's inference wallet, answered with a signed receipt; REST, MCP (`debitmydata_purchase`) and Bazaar description. People keep donating on debitmydata.com ("Inference $") | done (Base Sepolia; production needs `DMD_USDC_BASE_INFERENCE_ADDRESS` and the CDP facilitator) |
| Products bought for a DID-LLM: BottyBotty in three tiers (Entry 49, Standard 99, Premium 199 USD), each paid once and owned for good, a higher tier for the difference (`/v1/products`), paid in DMD, then a receipt signed by DebitMyData and the tier's entitlement on the DID-LLM; REST and MCP (`debitmydata_purchase`, `debitmydata_complete_purchase`) | done (on staging; off in production until BottyBotty's side is connected) |
| "Get a DID-LLM": the DMD payment as a QR code and an "Open in my DMD wallet" link (`debitmydata:` payment link) | done |
| "Get a DID-LLM" as an app for phones: on its own address (`DMD_GW_APP_HOST`, id.debitmydata.io) the page is served at the root with a web app manifest, icons and a service worker, to add to the home screen ([docs/production.md](docs/production.md#12-the-get-a-did-app-iddebitmydataio)); USDC listed first (greyed, "coming soon", while it is off); a short guide to what DMD is and how to get it | done (needs the DNS record) |
| The pages' top bar is white and stays at the top while scrolling, folds into a menu button on phones (no script needed), the DebitMyData name leads to the site's home, and the footer is in columns like the Agentic Spaces site; a "back to top" button appears as the page scrolls | done |
| DebitMyData's own logo (the coin side of debitmydata.com's logo, redrawn sharp) in the pages' top bar and as the favicon, with black binary lines; the app's icons show it with red lines (for dark home screens) in a white circle with a gold ring | done |
| The app offers itself on phones and tablets in a window centred on the screen, with its icon: never inside the app nor when the browser says it is installed (`getInstalledRelatedApps`, asked at every visit, so the offer comes back once the app is removed); only "Not now" is remembered in the browser, for 30 days (the cross or a tap outside just close it); a "Get the app" button in every page's footer opens it again, or says the app is already installed | done |
| "Get a DID-LLM" explains DID-LLMs before the steps, in the Agentic Spaces style: the numbers, what a DID-LLM is, why AI agents need one, how it works, with cards that rise into view and lift under the mouse or a tap; the app is named "DID-LLM DebitMyData" on the home screen | done |
| The name DID-LLM everywhere people and agents read it (pages, app, policies, API docs, MCP descriptions, error messages); identifiers stay as they are (`did:web`, `did`, `/get-did`, `dids` paths) | done |
| For AI agents: `/llms.txt` (how to register and pay by themselves, with this server's addresses and the payment options open now), linked from the Get a DID-LLM page's top, its footer and a no-script note; the page gives the MCP server's address | done |
| The pages' footer lists the three DMD explorers (Mempool explorer, Lite explorer, Rich list & API), as on debitmydata.io; once a DID-LLM is issued, the page offers "Join the Agentic Hub" | done |
| "Get a DID-LLM" shows a debit or credit card (Stripe) and PayPal greyed as coming soon, after USDC and DMD, until the gateway offers them | done |
| Card payments for DID-LLMs (payment option `card_stripe`): each quote gets its own Stripe payment page (Stripe Checkout, `payment_url`), open as long as the 31-minute quote; card details go to Stripe only; the worker checks each payment with Stripe itself (restricted key: Checkout Sessions write, PaymentIntents and Charges read), and settled payments for 30 days for refunds and disputes (`refunded`, `disputed`; a registration not yet issued is cancelled, an issued DID-LLM raises an alert); the page sends people to Stripe and continues by itself when they come back; agents get the link for their human (`pay_at_payment_url`) ([docs/card-payments.md](docs/card-payments.md)) | done (tested on staging in Stripe test mode; approved for production by DebitMyData on 2026-11-11, on once the production server has its live key) |
| Card payments always in US dollars: Stripe's prices in the payer's own currency (Adaptive Pricing, which adds a 2–4% conversion fee for the payer) are off on every payment page; "Get a DID-LLM" says the signature formats once | done |
| Own a DID-LLM with a key instead of a wallet: `scripts/did-owner-key.py` makes an Ed25519 owner key on the owner's computer and signs the registration's message (https gateways only, never for another key or gateway), with a guide ([docs/owner-key.md](docs/owner-key.md)); step 3 of "Get a DID-LLM" says how to continue after signing with a tool, and explains a pasted message instead of a signature | done |
| After a reload, "Get a DID-LLM" brings the registration in progress into view: it scrolls to it, puts the focus on Continue and makes it glow for a moment (no glow with reduced motion) | done |
| "Join the Agentic Hub" hidden after issuance until the hub's "Create Entity" saves entries (`SHOW_AGENTIC_HUB` in `get-did.js`) | done (back once the hub is ready) |
| Updates pause the monitor and the daily report (`deploy/*/update.sh`), so new settings are never read by the old code (no false "configuration cannot be loaded" alert); they start again at the end, even after a failed update | done |
| PayPal | waiting for DebitMyData's PayPal account access |
| The company's test DID-LLMs left out of the daily report's figures (`DMD_GW_REPORT_EXCLUDED_DIDS`); a payment option's rate-limit refusals alerted only when they last (3 checks in a row) | done |
| Naming: the payment option is "DebitMyData coin (native PoW chain)" and its asset DMD; the ticker is explained on the payment methods page, the price page, the MCP introduction and the API guide; the project, chain, nodes, explorer and alert subjects say DebitMyData | done |
| Production server package (`deploy/production`, [docs/production.md](docs/production.md)): DebitMyData node on the main chain holding only the treasury's watch-only key, every payment option off until switched on, nightly backup encrypted with an offline key to object storage (monitored), go-live checklist | done (needs the production server) |
| Credential signer (ID-15): in production the issuer key is held only by `dmd-gateway-signer` (own user, no network), which checks the database before signing; the API checks every signature | done |
| DID-LLM directory: owners list their DID-LLM with a public profile (the Agentic Hub's fields, no pictures: type avatar/robot/sentient, specialty, description, optional owner's name, region, country, creation date), signed with the DID-LLM's own key or wallet like the proof of control, and change or remove it the same way; plain text only (no links, e-mail addresses or hidden characters); shown while the DID-LLM is active; open API with CORS (`GET /v1/directory`, search, type, pages), pages `/directory`, `/directory/{id}` and `/directory/list`, MCP (`debitmydata_search_directory`, `debitmydata_list_in_directory`), `did-owner-key.py directory`; operators hide a listing (`dmd-gateway-admin directory hide`), and the monitor reports every new or changed listing; "Get a DID-LLM" offers it after issuance ([docs/directory.md](docs/directory.md)) | done |
| The top bar is wider than the page (1180 px) so every link fits on one line with "Directory" (staging's too), with the links a little closer on narrower screens and folded into the menu button below 900 px done |
| On staging, the directory page's owner-key command names the staging gateway (`DMD_GATEWAY_API=...`), and the tool says which gateway it asked when a message is not found there | done |
| The directory's pages have one address for people, the app's (id.debitmydata.io): on the API's address they lead there (308, same path and query), and `entry_url` and the alerts link there; the API stays on api.debitmydata.io | done |
| Signing with a key, on "Get a DID-LLM" (step 3) and the directory's listing page: the owner-key tool's command (with the registration or message ID, and `DMD_GATEWAY_API` off production) and a full-size **Continue** are the main way; pasting a signature made with other software is folded below | done |
| The owner-key tool takes the folder of key files (`~/did-keys`) and signs with the one that owns the DID-LLM, or says that none does (listing the keys it found) and signs nothing; the pages' commands give the folder | done |
| "Get a DID-LLM", step 1, "My agent's public key": how to make a key with the owner-key tool (the `new` command and the guide; on staging, a test key of its own and the guide's staging part) | done |
| The MCP server moves to `/agenticsuperhighway`, its name in DebitMyData's plans: the pages, `llms.txt`, discovery (`mcp_url`) and the API docs show the new address; `/mcp`, its first address, is still served with the same rate limits, so agents and listings that know it keep working. `llms.txt` points agents to Agentic Spaces for their own social pages and communities | done |
| A browser opening the MCP server's address (`/agenticsuperhighway`, or `/mcp`) gets a short page saying what it is: the address to give an MCP client, `llms.txt`, the REST API and "Get a DID-LLM" (before: an empty event stream, a blank page). MCP clients get exactly what they got before; the page is never cached (`no-store`, `Vary: Accept`) | done |
| Each server's `update.sh` refuses to run on the other server (by its settings' `DMD_GW_ENVIRONMENT`) before changing anything, and staging's `setup.sh` refuses production's settings: staging's test settings and test key can never land on production | done |
| One address for people, one for agents and developers (`api/hosts.py`): on the API's address (api.debitmydata.io), "Get a DID-LLM", the site's root and the policies lead to the app's (id.debitmydata.io), as the directory already did; on the app's address, the API docs (`/docs/advanced`, `/redoc`) lead to the API's. The menu's links therefore always land on the right address, and the installed app keeps people inside it. The API (`/v1/...`), `llms.txt`, the policies in Markdown and the MCP server answer at both, unmoved | done |
| MCP clients, crawlers and registries find the Agentic Superhighway without being told its address (`api/server_card.py`): its MCP Server Card (the MCP extension SEP-2127) at `/agenticsuperhighway/server-card` gives its name, version, what it does and how to connect (no tools: the server lists them itself), the AI Catalog at `/.well-known/ai-catalog.json` points to it, and DebitMyData's discovery runbook gets its two files in its own format (`application/json`): `/.well-known/mcp/server-card.json`, with the tools the server lists, and `/.well-known/mcp`, a short manifest; both point to the standard card. `server.json` lists it in the official MCP Registry as `io.debitmydata/agenticsuperhighway` ([docs/production.md](docs/production.md#13-the-official-mcp-registry)); a test keeps it the same as the card | done (the listing needs the DNS record) |
| No drop-down lists on the pages, so AI agents see every choice at once: "What is it for?" on Get a DID-LLM (in three groups, on an even grid) and the listing form's type are visible buttons, and the directory's types are links that keep the search words | done |
| OAuth phase 1, the authorization server (`api/oauth.py`, `oauth/`): on its own address (`DMD_GW_AUTH_HOST`, auth.debitmydata.io) an agent signs a short request with its DID-LLM's own key and gets a 10-minute access token for the MCP server (OAuth 2.1 client credentials with `private_key_jwt`, EdDSA; RS256 tokens, RFC 9068); metadata at `/.well-known/oauth-authorization-server`, keys at `/.well-known/jwks.json`; each signed request is used once; suspended and revoked DID-LLMs are refused. DID-LLMs owned by a wallet sign in with it (the runbook's wallet-signature path, `oauth/wallet_sign_in.py`): a message from `/challenge` (5 minutes, used once) signed with the wallet (EIP-191), for the same token (`urn:debitmydata:params:oauth:grant-type:wallet-signature`; in production the signer checks it again, ordinary wallets only). The MCP server accepts the tokens as an OAuth protected resource (`api/mcp_auth.py`, RFC 9728 metadata at `/.well-known/oauth-protected-resource/agenticsuperhighway`): optional, every claim checked on every request, 401 with `WWW-Authenticate` when a token fails; `debitmydata_whoami` answers who is signed in; the runbook's files list `oauth2.1`. The owner-key tool's `token` and `whoami` commands sign in ([docs/staging.md](docs/staging.md#9-oauth-phase-1-the-authorization-server)) | done: staging and production (auth.debitmydata.io), where the signer service signs the tokens and the API holds only the public key ([docs/production.md](docs/production.md#14-oauth-sign-in-authdebitmydataio)) |
| OAuth phase 2, people sign in to apps (MCP clients such as ChatGPT or Claude) with their DID-LLM in the browser (`api/oauth_browser.py`, `services/oauth_sign_in.py`, `oauth/clients.py`): authorization code with PKCE (S256), apps known by their Client ID Metadata Document (fetched carefully, no registration), one message signed with the DID-LLM's wallet or key (the owner-key tool's `sign-in`), `iss` in every answer (RFC 9207), refresh tokens used once (a replay ends the sign-in), `/revoke`, apps that sign their token requests (`private_key_jwt`); `debitmydata_whoami` called without a token is answered with HTTP 401 and the challenge, so apps offer the sign-in; a sign-in lasts 30 days from the person's signature (a grant from the token signer, `oauth/grants.py`) ([docs/staging.md](docs/staging.md#10-oauth-phase-2-people-sign-in-to-apps)) | done: staging and production, where the signer service checks each person's signature itself before it gives the grant, so smart wallets cannot sign in to apps there yet ([docs/production.md](docs/production.md#14-oauth-sign-in-authdebitmydataio)) |
| The discovery runbook's Day 5 handshake check (`dmd-gateway-handshake`, `handshake.py`): an automated client does what any agent does to find and use the Agentic Superhighway, unsupervised: the well-known files (200, `application/json`, the runbook's fields), the Server Card and AI Catalog, `llms.txt`, `initialize`, `tools/list` (every tool and parameter described, JSON Schema), the JSON-RPC errors (-32700, -32601, -32602), the sign-in negotiation when it is open (401 challenge, protected resource and authorization server metadata, keys, a wrong token refused), the same tools and version in every file and, with `--registry`, the official MCP Registry's listing; every update runs it on the server and fails if a check fails, and the daily report runs it through Cloudflare, with the Registry in production ([docs/staging.md](docs/staging.md#11-the-handshake-check-the-runbooks-day-5)) | done |
| What search engines, AI assistants and agents read about the site (`api/site_files.py`, `api/get_did_page.py`): at each name a `robots.txt` that welcomes them, with Content Signals (`search=yes, ai-input=yes, ai-train=yes`) and the sitemap (`/sitemap.xml`, the pages at that name; on staging, crawlers are asked to stay away and there is no sitemap); the API catalog (`/.well-known/api-catalog`, RFC 9727: the REST API with its OpenAPI description, docs, `llms.txt` and status, and the MCP server with its Server Card); on Get a DID-LLM, schema.org structured data (the DID-LLM as a Product with its price, from the registry, and image; DebitMyData; the site), Open Graph tags, a `Link` header (RFC 8288) to the API catalog, the OpenAPI description and `llms.txt`, and the page in Markdown for an agent that asks for it (`Accept: text/markdown`). The Server Card also gives `name`, `title`, `serverInfo`, `protocolVersion` and `transport`, and the AI Catalog each entry's `displayName` and `description` | done |
| One price text, word for word, wherever an agent looks: `$25.00 USD, paid once` on Get a DID-LLM (written by the server from the configuration, and again by the page's script), in `llms.txt` and its Markdown version, in `/v1/discovery` and in the MCP tools. The product comes first for agents that use the MCP server: the server's instructions (before any tool call), `debitmydata_discover` and `debitmydata_get_did_registration_offer` give its name, price, page and picture first, and how to buy it; `/v1/discovery`'s DID-LLM offer and `debitmydata_catalog` start with `name`, `price_text`, `product_url` and `image_url` | done |
| Agents open the checkout without buying anything: `debitmydata_open_checkout` (read-only, no arguments, creates and charges nothing) gives the checkout page where a person buys a DID-LLM (Get a DID-LLM's form), the payment options enabled now and the steps for an agent paying by itself; the server's instructions and `debitmydata_discover` point to it (`checkout_url`). The page at the MCP server's address also says what is sold there, with the same price text and structured data as Get a DID-LLM | done |
| Get a DID-LLM's buy button says what it does: **Continue to payment** (it fixes the exact amount and opens step 2, Pay), not "Get my quote", which reads as a request for a price | done |
| `debitmydata_open_checkout` ends with the purchase step, ready to fill in (`next_step`: `debitmydata_create_did_payment_quote` and its arguments), so an agent that opened the checkout knows what to call to buy | done |
| An Agent Skill to install (`api/agent_skills.py`): `/.well-known/agent-skills/index.json` (Agent Skills discovery 0.2.0) lists `debitmydata-did-llm`, a SKILL.md the gateway writes itself from llms.txt (when to use it, the quickest path through the MCP server, then the full text), with the SHA-256 digest of the very text it serves; it links to DebitMyData's shared skill (debitmydata-skill) | done |
| Owner changes and owner revocation (ID-12; `services/owner_changes.py`, `api/owners.py`): an owner moves a DID-LLM to new owners (a new key and/or wallet) with `POST /v1/dids/{did}/owner-changes`, MCP `debitmydata_change_owner` or the owner-key tool's `change-owner`: one current owner signs the change's "approve" message and each new owner its "accept" message, within 15 minutes; then only the new owners control it (DID document, record, proofs of control), every sign-in made before ends (apps' refresh tokens revoked; older access tokens refused by the API, older app sign-ins by the token signer), and the monitor reports it. An owner revokes the DID-LLM for good with one signed message (`/revocations`, `debitmydata_revoke_did`, `revoke`). MCP tool descriptions lose the code's indentation on Python 3.12 too | done |
| The identity endpoint (`/agenticsuperhighway/identity`, `mcp_server.build_identity_server`): a separate MCP server with three read-only tools, each for a signed-in DID-LLM (`debitmydata_whoami`; `debitmydata_get_profile`, the DID-LLM's record, owners, credentials, entitlements and listing; `debitmydata_verify_agent` with the DID only), for apps that need nothing else, such as a Google Connected App: every request there needs the token, from `initialize` on (0.49.1), so a client signs in as it connects; tokens are issued for one endpoint or the other (`resource`), each refusing the other's. Ending an app's sign-in (`/revoke` with its refresh token or an access token, a replayed refresh token) ends its access tokens at once: they carry their sign-in (`sid`), checked on every request. Google's domain-verification file at `/.well-known/google-connected-apps` (`DMD_GW_GOOGLE_CONNECTED_APPS_FILE`) | done |
| Tool names in full: every MCP tool's name starts with `debitmydata_` (0.50.0), the company's name, never shortened. The earlier `dmd_` names still work when called, on both endpoints, but only the new ones are listed, so apps and scripts written before keep working. Identifiers stay as they are (`dmd_identity_...` and the other ID prefixes, payment option IDs such as `dmd_pow_native`, the `DMD_GW_` settings): they are inside issued credentials and records, and an `identity_id` never changes | done |
| PayPal payments for DID-LLMs (payment option `paypal`, 0.51.0): each quote gets its own PayPal order and payment page (PayPal Checkout, Orders v2, `payment_url`), for exactly 25.00 USD, eCheck refused; the payer's PayPal login goes to PayPal only; the worker captures an approved order only while its 31-minute quote is open (never twice), checks the capture with PayPal itself, and settled payments for 30 days for refunds and disputes; PayPal's sandbox on staging; "Get a DID-LLM" offers it in place of the greyed "Soon" entry once it is on ([docs/paypal-payments.md](docs/paypal-payments.md)) | done (on once DebitMyData's PayPal app is set) |
| Google Connected Apps' sign-in (0.52.0): a registration endpoint (RFC 7591, `/register` at the auth host, off until `DMD_GW_OAUTH_REGISTRATION_ENABLED`) for Google's own return addresses only, at most 30 an hour, unused ones forgotten after 7 days; registered apps authenticate with a client secret (basic or post, kept as its SHA-256), get tokens for the identity endpoint only, and keep one refresh token (Google expects it back unchanged) until the sign-in's 30 days end; the person signs "Sign in to: ... (oauth-redirect.googleusercontent.com)", checked by the token signer as for every app | done (on once Google's console is set up) |
| Native DMD wallet proofs (DEC-07); lost-key recovery (ID-14) | next |

Every payment rail is **off** by default. The native DMD rail can be switched on for a
private regtest chain (see [docs/regtest-demo.md](docs/regtest-demo.md)); on mainnet it is
priced by the higher of a minimum rate (0.001 USD per DMD) and the cost of mining DMD, and
runs in production with the gateway's own DebitMyData node and its watch-only receiving wallet. The x402 USDC rail runs on Base Sepolia on staging; on
Base mainnet it pays DebitMyData's receiving address and settles through Coinbase's CDP
facilitator, which needs DebitMyData's CDP API key (DEC-08). Card payments run in Stripe's
test mode on staging once its restricted test key is set, and in production with its
restricted live key ([docs/card-payments.md](docs/card-payments.md)); PayPal payments run in
PayPal's sandbox on staging and in production with DebitMyData's live PayPal app
([docs/paypal-payments.md](docs/paypal-payments.md)). A staging server with the full flow on a private
regtest chain runs at `https://staging-api.debitmydata.io`: anyone can try the registration
from its [guided test page](https://staging-api.debitmydata.io/docs), or step by step from
the [advanced API docs](https://staging-api.debitmydata.io/docs/advanced) (see
[docs/staging.md](docs/staging.md)).
See [docs/roadmap.md](docs/roadmap.md) for the task list and decisions, and
[docs/integrations.md](docs/integrations.md) to connect a product: agents and people signing
in with their DID-LLM (OAuth), checking an agent, owner changes, testing on staging.

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

## Registration flow

| Step | Endpoint |
| --- | --- |
| Discover the offer and enabled payment options | `GET /v1/discovery`, `GET /v1/payment-options` |
| Get a quote (fixed 25.00 USD, exact amount in the chosen asset) | `POST /v1/dids/registration-quotes` |
| Pay (DMD), then follow the payment | `GET /v1/payments/{payment_intent_id}` |
| Get a one-time message to sign with the controller key or wallet | `POST /v1/dids/registrations/{registration_intent_id}/controller-challenges` |
| Send the signature | `POST /v1/dids/registrations/{registration_intent_id}/controller-proofs` |
| See what is still missing | `GET /v1/dids/registrations/{registration_intent_id}` |
| Receive the DID-LLM, signed credential and entitlements (x402: pay here, see below) | `POST /v1/dids/registrations` |
| Optionally report the payment transaction (a hint only) | `POST /v1/payments/{payment_intent_id}/references` |
| Look them up | `GET /v1/dids/{identity_id}`, `GET /v1/credentials/{credential_id}`, `GET /v1/entitlements/{identity_id}` (the identity ID or the DID-LLM itself) |
| Resolve the DID-LLM and the issuer key | `GET /agents/{id}/did.json`, `GET /.well-known/did.json` |
| Read the terms, privacy, payment, refund, DID-LLM and security policies | `/policies`, `GET /v1/policies` |
| Optional: list the DID-LLM in the directory (signed by its owner), or read the directory | `POST /v1/directory/{did}/challenges`, `POST /v1/directory/{did}/proofs`, `GET /v1/directory` |

The same steps are available as MCP tools at `/agenticsuperhighway` (Streamable HTTP; `/mcp`,
its first address, still works): `debitmydata_discover`,
`debitmydata_get_did_registration_offer`, `debitmydata_open_checkout`,
`debitmydata_list_supported_payment_options`, `debitmydata_create_did_payment_quote`,
`debitmydata_submit_payment_reference`, `debitmydata_get_payment_status`,
`debitmydata_prove_wallet_control`, `debitmydata_register_did`,
`debitmydata_get_registration_status`, `debitmydata_catalog`, `debitmydata_get_purchase_quote`,
`debitmydata_purchase`, `debitmydata_marketplace_provider_requirements`,
`debitmydata_search_directory`, `debitmydata_list_in_directory`, `debitmydata_verify_agent`,
`debitmydata_change_owner` and `debitmydata_revoke_did`. Each tool calls the REST API above
in-process, so both give the same results and error codes. Its Server Card,
`/agenticsuperhighway/server-card` (listed in `/.well-known/ai-catalog.json`), tells MCP clients
and registries what it is and how to connect.

**Verifying an agent by its DID-LLM** (the runbook's `verify_agent`; MCP
`debitmydata_verify_agent`). Any agent or service checks a DID-LLM it meets:
`POST /v1/dids/verifications` with `did` (and `audience`, its own name) answers whether
DebitMyData issued it and it is active, its credential checked against DebitMyData's issuer key,
its owner key or wallet, its entitlements, and a `challenge`: a "DebitMyData proof of control"
message, valid 10 minutes, naming the DID-LLM, the verifier and a nonce. The agent signs it with
its DID-LLM's key or wallet; the same request with `message` and `signature` adds `proof`
(whether the signature is the owner's now, with the nonce to compare). `verified` sums it up,
`reasons` says why not. Only messages in this form are checked, so a signature made to sign in
to DebitMyData is never taken as a proof for someone else.

**Inference donations (agents).** `POST /v1/donations/inference` with
`{"amount_usd": "5.00"}` (from 0.50 to 1,000.00 USD; optionally `donor_name` and
`donor_did`) answers `402` with the x402 terms for exactly that amount, paid to
DebitMyData's inference wallet (`DMD_USDC_BASE_INFERENCE_ADDRESS`, not the DID-LLM payment
address). Send the same body again with the signed payment in `PAYMENT-SIGNATURE`: no quote
is needed, the payment is checked on chain like a DID-LLM payment, and the answer (`201`) carries
a receipt signed by DebitMyData (a `DebitMyDataDonationReceipt` credential), or `202` with a
`status_url` (`GET /v1/donations/{purchase_id}`) while confirmations come in. Sending the
same payment again returns the same donation. Through MCP: `debitmydata_catalog` lists
`inference_donation`, and `debitmydata_purchase` takes the amount in `details` and the payment in
`x402_payment` or `_meta["x402/payment"]`. Donations appear under `donations` in
`/v1/discovery`.

**Paying with x402 (USDC on Base).** Quote with `"preferred_payment_option": "x402_usdc_base"`,
prove the controller key, then call `POST /v1/dids/registrations`. It answers `402` with a
`PAYMENT-REQUIRED` header (x402 version 2, scheme `exact`: the quoted amount in USDC base units,
the USDC contract, the network and DebitMyData's receiving address, plus a Bazaar discovery
description). Call it again with the signed payment in the `PAYMENT-SIGNATURE` header: the
gateway checks it against the quote, the facilitator verifies and settles it, the gateway checks
the settlement on chain itself, and the DID-LLM comes back in the same answer with a
`PAYMENT-RESPONSE` header (or `202` if the confirmations take longer; then call again without
paying). Payment is asked for only once everything else is ready, so an agent is never charged
for a DID-LLM that cannot be issued. Any standard x402 client can pay it; through MCP,
`debitmydata_register_did` returns the payment requirements and takes the payment in
`x402_payment` or `_meta["x402/payment"]`.

DID-LLM issuance needs a credential signing key (`DMD_GW_ISSUER_KEY_PATH`, created with
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
tests/
docs/                        architecture, roadmap, operator checklists
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

## Reporting security issues

Please report vulnerabilities privately, never in public issues. See
[SECURITY.md](SECURITY.md).

## Database notes

`audit_events`, `payment_events`, `credited_outputs`, `controller_proofs` and
`payment_references` are append-only: database triggers reject `UPDATE`, `DELETE` and `TRUNCATE`. `credited_outputs` has a primary key on
(network, transaction, output index), so a payment output can be credited to only one
intent. In staging and production, run migrations as a separate owner role and let the
application connect as a role that has only the privileges it needs.
