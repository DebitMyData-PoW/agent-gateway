# Production server

The production gateway runs at `https://api.debitmydata.io` on its own Vultr server, in
DebitMyData's account, separate from staging and from every other DebitMyData server. It
takes real payments, so it has none of staging's test material: no test tools, no test
chain, no test wallets. Every update is tried on staging first.

```
Internet ── Cloudflare proxy (TLS, WAF) ──► VPS :443, open to Cloudflare only
                                             nginx (Cloudflare origin certificate)
                                               └► gateway API + MCP (127.0.0.1:8000)
                                                    ├► payment worker, monitor, daily report
                                                    ├► credential signer (own user, no network,
                                                    │    the only holder of the issuer key)
                                                    ├► PostgreSQL 16 (local)
                                                    ├► DebitMyData node, main chain (RPC on
                                                    │    127.0.0.1 only, no inbound peers),
                                                    │    holding only the treasury's watch-only key
                                                    └► Base mainnet: Coinbase's x402 facilitator
                                                         and a Base RPC, over HTTPS
Nightly: database + settings, encrypted with an offline key ──► Vultr Object Storage
```

**Steps 1 to 4 prepare the server; 5 to 9 switch payments on, one at a time.**

## 1. Before you start

- **Server:** Vultr, DebitMyData's account. Ubuntu 24.04 LTS, 4 vCPU, 8 GB RAM (the same plan
  as staging), automatic backups on. SSH on your own port, allowed in UFW, and UFW active.
- **DNS:** in Cloudflare, `api.debitmydata.io` points to the new server, proxied (orange
  cloud). At go-live (step 9), remove the temporary rule that redirects
  `api.debitmydata.io/policies` to staging.
- **Certificate:** a Cloudflare origin certificate covering `api.debitmydata.io` (staging's
  `*.debitmydata.io` certificate works if it is a wildcard): save it as
  `/etc/ssl/cloudflare/origin.pem` and its key as `/etc/ssl/cloudflare/origin.key`. SSL/TLS
  mode: Full (strict).
- **DebitMyData Core v1.3.5:** copy the official `debitmydatad` and `debitmydata-cli` to
  `/usr/local/bin/` (`chmod 755`), after checking their SHA-256 against the release.

## 2. Install

```bash
git clone https://github.com/DebitMyData-PoW/agent-gateway.git /opt/agent-gateway
/opt/agent-gateway/deploy/production/setup.sh
```

The script installs everything, creates the secrets once (database and node passwords, the
issuer key) and starts:

| Service | What it does |
| --- | --- |
| `debitmydata` | the DebitMyData node on the main chain (`dmd-cli ...` to talk to it) |
| `dmd-gateway-signer` | the credential signer (below) |
| `dmd-gateway-api`, `dmd-gateway-worker` | the API and the payment worker |
| `dmd-gateway-monitor.timer`, `-report.timer` | alerts every 2 minutes, daily report at 07:00 UTC |
| `dmd-gateway-backup.timer` | the nightly backup at 03:30 UTC (step 4) |

**The credential signer.** The key that signs every DID credential and donation receipt is
`/etc/dmd-signer/issuer-key.pem`, readable only by the `dmd_signer` user. The signer has no
network access; the API asks it for each signature through a local socket. Before signing,
the signer checks the database itself (through a read-only role): only a settled payment with
a verified controller proof gets a DID credential, once; only a settled donation gets a
receipt, once. Even a fully compromised API could neither copy the key nor have it sign
something that was not paid. The API checks each signature against the public key in its
settings (`DMD_GW_ISSUER_PUBLIC_KEY`), which is published at `/.well-known/did.json`.

The node then downloads the DebitMyData chain. Follow it with:

```bash
dmd-cli getblockchaininfo | jq '{blocks, headers, verificationprogress}'
```

It is ready when `blocks` equals `headers` and `verificationprogress` is close to 1, and
`blocks` matches https://explorer.debitmydata.io.

## 3. Alerts

Copy the alert lines (`DMD_GW_ALERT_*`, `DMD_GW_REPORT_EMAIL_TO`, `DMD_ALERT_SMTP_PASSWORD`,
`DMD_ALERT_DISCORD_WEBHOOK_URL`) from staging's `/etc/dmd-gateway/gateway.env` into
production's, then send a test message as in [monitoring.md](monitoring.md). Messages from this
server start with `[DebitMyData Gateway production]`.

Add an outside check too, since the monitor cannot report its own server being down: any
uptime service (for example UptimeRobot's free plan) checking
`https://api.debitmydata.io/readyz` every 5 minutes, with an email when it does not answer
`"ready"`.

## 4. Backups

Every night the server makes two files, each encrypted with [age](https://age-encryption.org)
to a public key whose private half never touches the server:

- the database (`agent-gateway-db-<time>.dump.age`);
- the settings and keys, including the issuer key (`agent-gateway-config-<time>.tar.age`).

They go to Vultr Object Storage; the last 7 days also stay on the server. Vultr's own server
backups come on top of this.

**a. The backup key (once, on your own computer, never on the server).** On the Ubuntu VM
(`sudo apt install age`):

```bash
age-keygen -o dmd-gateway-backup-key.txt
```

It prints the public key (`age1...`). Save the whole file in your password manager as
"DMD gateway backup key", plus a printed copy with the treasury records. Without it, no backup
can be read; with it, every backup can: keep it like the treasury's recovery key. Then
delete the file from the computer.

**b. Object storage.** In Vultr, add Object Storage in the server's region. Create a bucket (for example `dmd-gateway-backups`), and note the hostname (for example
`ewr1.vultrobjects.com`), the access key and the secret key.

**c. Settings,** in `/etc/dmd-gateway/backup.env` on the server (`chmod 600`):

```bash
DMD_BACKUP_AGE_RECIPIENT=age1...
DMD_BACKUP_REMOTE=vultr:dmd-gateway-backups/production
DMD_BACKUP_KEEP_DAYS=30
RCLONE_CONFIG_VULTR_TYPE=s3
RCLONE_CONFIG_VULTR_PROVIDER=Other
RCLONE_CONFIG_VULTR_ENDPOINT=https://ewr1.vultrobjects.com
RCLONE_CONFIG_VULTR_ACCESS_KEY_ID=...
RCLONE_CONFIG_VULTR_SECRET_ACCESS_KEY='...'
```

**d. First run and check:**

```bash
systemctl start dmd-gateway-backup
journalctl -u dmd-gateway-backup -n 20 --no-pager
```

It ends with `Backup <time>: ... copied to vultr:...`. From then on the monitor warns when no
backup has succeeded for 26 hours.

**e. Restore test (once now, then every 6 months),** on a scratch server or the Ubuntu VM,
never on production. Copy a pair of files from the bucket and the key file, then:

```bash
age -d -i dmd-gateway-backup-key.txt agent-gateway-config-<time>.tar.age | tar -tvf -
age -d -i dmd-gateway-backup-key.txt agent-gateway-db-<time>.dump.age > db.dump
createdb restore_test && pg_restore -d restore_test db.dump
psql -d restore_test -c "SELECT count(*) FROM identities"
```

The settings list and the DID count must match the server. Delete the scratch copies after.

**Restoring production** after losing the server: a new server (step 1), then put back
`/etc/dmd-gateway`, `/etc/dmd-signer` and `/etc/debitmydata` from the settings backup before
running `setup.sh`, then `pg_restore --clean -d agent_gateway` the database backup. Keeping
the same issuer key matters: every credential already issued is checked against it. The
treasury's watch-only key is loaded again from `dmd-treasury-public.json` (treasury guide,
section 5).

## 5. Before any payment

- `https://api.debitmydata.io/readyz` answers `"ready"`, including `"credential_signer":
  "ok"`.
- `https://api.debitmydata.io/.well-known/did.json` shows the issuer key the setup printed.
- `https://api.debitmydata.io/v1/discovery` and `/policies` answer; `/v1/payment-options`
  lists nothing yet.

## 6. Switch on DebitMyData coin (DMD) payments

1. The node is fully synced (step 2).
2. Load the treasury's watch-only key: **DMD Treasury Wallet: Offline Setup**, section 5.
3. In `/etc/dmd-gateway/gateway.env`: `DMD_RAIL_DMD_POW_NATIVE_ENABLED=true`, then
   `systemctl restart dmd-gateway-api dmd-gateway-worker`.
4. Check, as in section 6 of the treasury guide: a DID quote paid in DMD gets **Address 0**
   from the record sheet, and the monitor's *A new receiving destination is in use* line
   equals the record sheet's *Daily report shows* line.

## 7. Switch on USDC payments (x402 on Base)

1. Put the Coinbase Developer Platform key in `/etc/dmd-gateway/gateway.env`, in single quotes:
   `DMD_X402_CDP_API_KEY_ID='...'` and `DMD_X402_CDP_API_KEY_SECRET='...'`.
2. `DMD_RAIL_X402_USDC_BASE_ENABLED=true`, then
   `systemctl restart dmd-gateway-api dmd-gateway-worker`. If the key cannot be read, the
   gateway refuses to start and says why (`journalctl -u dmd-gateway-api`): fix the key, or
   set the flag back to `false`.
3. Check the paid routes with Coinbase's Bazaar validator:
   `cd /opt/agent-gateway && .venv/bin/python scripts/validate_bazaar.py https://api.debitmydata.io`.
4. One real DID payment in USDC, end to end, checked on basescan.org: the USDC arrives in
   AgenticSpaces DID-LLM (`0x67C1...2eC3`).

`https://mainnet.base.org` is Base's public RPC; it is rate-limited, so under real traffic use
a dedicated Base RPC endpoint (`DMD_BASE_RPC_URL`).

## 8. Updates

**The "Get a DID" page.** `/get-did`, the same address as on staging and the one to link
from DebitMyData's sites (`/docs` and the site's address lead there), takes people through a
registration in the debitmydata.com look: the
DID's details and the quote, the payment (the exact amount and address, with the payment
watched until it settles), one message signed with their wallet (a **Sign with my wallet**
button for browser wallets, or a signature pasted from another wallet or the agent's tools),
and the DID. It never asks for a private key or a recovery phrase and never sends money; it
keeps the registration's IDs in the browser so that a reload continues where it stopped. USDC
is not on the page yet (agents pay it through the API): until step 7 switches it on, the page
lists it first, greyed, as coming soon, and DMD is the one chosen; the API lists only what can
be paid with now. Its font, Mada, is served by the
gateway (SIL Open Font License, `src/dmd_gateway/static/fonts/OFL-Mada.txt`).

**The same look on every page for people.** The "Get a DID" page, the advanced API docs, the
policy pages (`/policies`) and staging's guided test page share one top bar (the pages, with
the current one marked), one footer (the policies and a contact address) and the
debitmydata.com colours and font (`src/dmd_gateway/static/brand.css` and
`src/dmd_gateway/api/brand.py`). On the advanced API docs, the endpoints that act (POST) are
red and those that only read (GET) grey.

**The advanced API docs.** `/docs/advanced` is the Swagger UI page, with bigger text, fields
and buttons than Swagger UI's own. It opens like the guided pages: the registration's steps
as cards, in the order of the payment option picked (DMD, or USDC once it is open), the next
one with what to paste into it and an **Open step N** button; the full written guide (how to
register a DID by hand: paying and signing happen in the agent's own wallet and tools, never
in the page) is folded under them, and every endpoint is below. Each step's endpoint is marked
**Step N**, and the panel at the bottom right shows the next step with a **Go** button that
opens it (or, for paying and signing, what to do outside the page) and what to paste into it,
and keeps the values each answer gives, each with a **Copy** button, in the open page only.
It and `/redoc` load Swagger UI and ReDoc from jsDelivr at exact versions, and the browser
checks each file's SHA-384 fingerprint before running it; their Content-Security-Policy lets
nothing else run. To move to newer versions, see the top of
`src/dmd_gateway/api/docs_pages.py`.

Only with a version that already runs well on staging:

```bash
/opt/agent-gateway/deploy/production/update.sh
```

It keeps every secret, key, wallet and setting, and refuses to run on a server whose
settings are not production's (it changes nothing there). Staging's `update.sh` likewise
refuses to run here.

**A new DebitMyData Core release** (staging first, then production). Check the SHA-256 of the
new `debitmydatad` and `debitmydata-cli` against the release, then:

```bash
dmd-cli listwallets          # note the wallets loaded now
systemctl stop dmd-gateway-worker debitmydata
install -m 755 debitmydatad debitmydata-cli /usr/local/bin/
systemctl start debitmydata
dmd-cli -rpcwait getnetworkinfo | jq -r .subversion   # the version now running
dmd-cli listwallets          # the same list as before
systemctl start dmd-gateway-worker
```

If a wallet is missing from the list, load it with `dmd-cli loadwallet <name> true` (`true`
makes the node load it at every start from now on) before starting the worker. The wallets
and the chain data stay as they are: from v1.3.3 to v1.3.5, the main chain's rules and the
data formats did not change. The monitor may send one node alert during the restart.

## 9. Suspending, reactivating or revoking a DID

When a DID is misused (impersonation, fraud, a compromised key, a payment that left the chain)
or its holder asks, change its status on the server with `dmd-gateway-admin`. Nothing on the
public API can do it. The [DID policy](https://api.debitmydata.io/policies/did) says when DebitMyData may.

```bash
sudo dmd-gateway-admin did show <DID>
sudo dmd-gateway-admin did suspend <DID> --reason "Reported impersonation, under review" --by "Tyler"
sudo dmd-gateway-admin did reactivate <DID> --reason "Review closed: no misuse" --by "Tyler"
sudo dmd-gateway-admin did revoke <DID> --reason "Fraud confirmed" --by "Tyler"
```

`<DID>` can be the DID (`did:web:api.debitmydata.io:agents:...`), its `did.json` address, its
identity ID (`dmd_identity_...`) or its registration ID (`dmd_did_intent_...`).

- **Suspend** is temporary: the DID document answers **410 Gone**, the DID's entitlements
  and credential show as suspended, until it is reactivated.
- **Revoke** is permanent: it asks you to type the end of the identity ID to confirm, and
  revokes the credential and the entitlements too. A revoked DID never comes back.
- Every change is written to the audit trail with the reason and the name given in `--by`
  (the database refuses to edit or delete it), and the monitor sends an alert within two
  minutes. `did show` prints the DID's whole history. Never put secrets or personal data in a
  reason: it is kept for good and sent in the alert.
- The registration status and `dmd_get_registration_status` (MCP) show the DID's status in
  `did_status`, the identity record in `status`. The payment is not refunded (see the
  [refund policy](https://api.debitmydata.io/policies/refunds)).

## 10. Names reserved for DebitMyData

A DID's display name is public, so the gateway refuses, when the quote is requested and
before anything is paid:

- names with hidden characters (zero-width or text-direction marks, unusual spaces);
- words that mix alphabets, such as "DebitMyData" written with a Cyrillic "е" (names written
  entirely in another alphabet are fine);
- names that look like DebitMyData's own: DebitMyData, DMD, BottyBotty, AgenticSpaces, also
  written with other capitals, spaces, accents, lookalike letters or digits ("D3bit My Data").

DebitMyData's own DIDs may use those names when their controller is listed in
`/etc/dmd-gateway/gateway.env`, separated by commas, then `systemctl restart dmd-gateway-api`:

```bash
DMD_GW_BRAND_NAME_CONTROLLERS=ed25519:z6Mk...,0x...
```

List only keys and wallets that DebitMyData holds (agent keys `ed25519:...`, wallets `0x...`).
The other checks still apply to them. To see whether a DID issued earlier has a name that
would now be refused, and suspend it if it impersonates DebitMyData:

```bash
sudo dmd-gateway-admin names check
```

## 11. BottyBotty (a product for a DID)

Agents with a DID can buy BottyBotty for it in one of three tiers, each paid once and owned
for good: Entry (49.00 USD), Standard (99.00 USD) and Premium (199.00 USD); a higher tier costs
the difference. A quote, a DMD payment like the DID's, then a receipt signed by DebitMyData and
the tier's entitlement (`bottybotty_entry`, `bottybotty_standard` or `bottybotty_premium`) on
the DID ([architecture.md](architecture.md#products-for-a-did-bottybotty)). At the minimum DMD
rate (0.001 USD per DMD) the tiers cost 49,000, 99,000 and 199,000 DMD. The prices are in
`config/payment_routes.yaml` under `products:`; a change there comes with a gateway update. The
terms and the refund policy cover it (version 2026-11-03: owned for good with the DID,
non-refundable once completed).

It is **off** until switched on, and stays off in production until BottyBotty (Suyash's team)
has published what each tier includes and activates the Agentics that own a tier, so that
nothing is sold before it is delivered.

Then switch it on and check it is offered:

```bash
echo "DMD_PRODUCT_BOTTYBOTTY_PROTECTION_ENABLED=true" | sudo tee -a /etc/dmd-gateway/gateway.env
sudo systemctl restart dmd-gateway-api
curl -s https://api.debitmydata.io/v1/products
```

To stop selling it, set the value to `false` and restart the API: purchases already paid
can still be completed, and tiers already owned stay with their DIDs. A purchase
paid for a DID that is suspended before it is completed waits until the DID is reactivated;
if the DID is revoked, the purchase stays paid without a receipt and is refunded by hand as
the refund policy says.

## 12. The Get a DID app (id.debitmydata.io)

The "Get a DID" page is also an app people add to their phone's home screen and open full
screen: on its own address, `https://id.debitmydata.io`, the same gateway serves the page at
the root, with the app's name, icons and a small service worker that keeps the page for opening
without a network (it never touches the API calls). DIDs keep `api.debitmydata.io` in their
name. Once the app's address is set, the pages for people (Get a DID-LLM, the directory, the
policies) have that one address: at `api.debitmydata.io` they lead there (`/get-did` to the
app's root), and the API docs lead the other way, to `api.debitmydata.io`. The API,
`llms.txt`, the policies in Markdown and the MCP server answer at both. People install it:

- **iPhone (Safari):** Share, then **Add to Home Screen**;
- **Android (Chrome):** the **Install app** prompt, or the menu, then **Install app**.

On phones and tablets the page itself offers the app, in a window centred on the screen: the
browser's own prompt can stay away for months after a "No thanks" or after the app was removed.
Where the browser can install it (Chrome on Android), the window has an **Install the app**
button; elsewhere it shows the two steps by hand. It never shows inside the app, nor when the
browser says the app is installed, which the page asks at every visit: Chrome on Android tells
it (the manifest lists the app under `related_applications`), so the offer comes back as soon
as the app is removed. Safari on an iPhone can't tell, so there the window adds "Already added it? Open it from your
home screen". Only **Not now** is remembered, in the browser's own storage (not in a cookie),
for 30 days (`DECLINE_DAYS` in `get-did.js`); the cross, a tap outside the window or Back only
close it until the page is opened again.

Every page's footer has a **Get the app** button, on any device (hidden inside the app): it
leads to the app's page and opens the window there, even after a **Not now**, which it forgets.
When the browser says the app is installed, the window says so instead.

Every page (Get a DID, the guided test, the API docs and the policies) has a light and a dark
theme: light unless the visitor picks dark with the moon / sun button in the top bar. The choice
is kept for a year in the `dmd-theme-id` cookie, for this site only (the explorer, the mining
pool and debitmydata.io each remember their own choice), and the privacy policy says so. The
small script that applies it before the page is drawn is `static/theme.js`, allowed by its hash
in every page's Content-Security-Policy (`brand.THEME_SCRIPT_SOURCE`). The dark theme's colours
are at the end of `static/brand.css`; the API docs page adds its own for Swagger UI.

The app's service worker keeps the page, the font and the icons for opening without a network.
Its cache is named after those files' content and it fetches them past the browser's own cache,
so a new icon or logo reaches people who already opened the page, with nothing to raise by hand.

The app's identity is its address: once people have installed it, the host must not change.

1. **Cloudflare, DNS:** add a record `id`, the same type and target as `api` (the server's
   address), **Proxied** (orange cloud).
2. **Certificate:** the server's Cloudflare origin certificate must cover the name. A
   certificate made for `*.debitmydata.io` already does; check on the server:

   ```bash
   sudo openssl x509 -in /etc/ssl/cloudflare/origin.pem -noout -ext subjectAltName
   ```

   If neither `id.debitmydata.io` nor `*.debitmydata.io` is listed, create a new origin
   certificate in Cloudflare (SSL/TLS, Origin Server) for `api.debitmydata.io` and
   `id.debitmydata.io` and replace both files, as at installation.
3. **Setting:** add the host name to `/etc/dmd-gateway/gateway.env`, then run the update
   (step 8), which adds the name to nginx and checks that the app answers:

   ```bash
   echo "DMD_GW_APP_HOST=id.debitmydata.io" | sudo tee -a /etc/dmd-gateway/gateway.env
   ```

4. **Check** from your computer: `https://id.debitmydata.io` shows the page, and
   `https://id.debitmydata.io/manifest.webmanifest` the app's name and icons.

To try it on staging first, do the same there with another name, for example
`id-staging.debitmydata.io`.

## 13. The official MCP Registry

MCP clients and crawlers can find the Agentic Superhighway by themselves: its **Server Card**
(the MCP extension SEP-2127), at `https://api.debitmydata.io/agenticsuperhighway/server-card`,
gives its name, version, what it does and how to connect, and the site's **AI Catalog**,
`https://api.debitmydata.io/.well-known/ai-catalog.json`, points to it. DebitMyData's
discovery runbook (the "AgenticSuperhighway Discovery & Publishing SOP") gets its two files in
its own format, written for earlier drafts of MCP discovery: `/.well-known/mcp/server-card.json`
(with the tools the server lists) and `/.well-known/mcp`. All come with every update; check
them after one (the last three lines are the runbook's Day 1 checks):

```bash
curl -s https://api.debitmydata.io/agenticsuperhighway/server-card
curl -s https://api.debitmydata.io/.well-known/ai-catalog.json
curl -sf https://api.debitmydata.io/.well-known/mcp/server-card.json | jq '.mcp_version'
curl -sf https://api.debitmydata.io/.well-known/mcp | jq '.endpoints[0].url'
curl -sI https://api.debitmydata.io/.well-known/mcp/server-card.json | grep -i "content-type: application/json"
```

The **official MCP Registry** (registry.modelcontextprotocol.io), where MCP clients and
directories look for servers, lists it from `server.json` at the repository root, under the
name `io.debitmydata/agenticsuperhighway`. A test keeps that file the same as the card, and
its version is the gateway's. The Registry only accepts the name from whoever can prove they
control debitmydata.io: with a DNS record that holds a public key, whose private key stays on
the computer you publish from (like the SSH signing key, it never leaves it).

**Once**, on the computer with your copy of the repository:

1. Install the publisher, the Registry's own tool:

   ```bash
   curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" | tar xz mcp-publisher && sudo mv mcp-publisher /usr/local/bin/
   mcp-publisher --help | head -3
   ```

2. Make the key, and print the DNS record's value (the public key only):

   ```bash
   mkdir -p ~/.mcp-registry && chmod 700 ~/.mcp-registry
   openssl genpkey -algorithm Ed25519 -out ~/.mcp-registry/key.pem && chmod 600 ~/.mcp-registry/key.pem
   echo "v=MCPv1; k=ed25519; p=$(openssl pkey -in ~/.mcp-registry/key.pem -pubout -outform DER | tail -c 32 | base64)"
   ```

3. **Cloudflare, DNS, debitmydata.io:** add a record of type **TXT**, name `@` (debitmydata.io
   itself: the Registry does not look at a subdomain), content the line printed above
   (`v=MCPv1; k=ed25519; p=...`). It changes nothing else: a domain can have several TXT
   records. Check after a minute: `dig +short TXT debitmydata.io` shows it.

**To publish** (the first time, and after an update when the listing should show the new
version; a version can be published only once):

```bash
cd /root/agent-gateway
mcp-publisher login dns --domain debitmydata.io --private-key "$(openssl pkey -in ~/.mcp-registry/key.pem -noout -text | grep -A3 'priv:' | tail -n +2 | tr -d ' :\n')"
mcp-publisher publish
curl -s "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.debitmydata/agenticsuperhighway"
```

The last command shows the listing. The login lasts a while; when `publish` says it has
expired, log in again.

Optional: debitmydata.io's own site can point to the card too, with the same AI Catalog at
`https://debitmydata.io/.well-known/ai-catalog.json` (the website's team), so that crawlers
that start from the company's domain find it.

## 14. OAuth sign-in (auth.debitmydata.io)

Agents can sign in to the Agentic Superhighway with their DID-LLM: they sign a short request
with the DID-LLM's own key at `https://auth.debitmydata.io/token` and get an access token,
valid for 10 minutes, for the MCP server. It is optional (registering a DID-LLM never needs
it) and every claim is checked on every request. How it works, and the same steps on staging:
[staging.md, section 9](staging.md#9-oauth-phase-1-the-authorization-server).

In production the **signer service signs the tokens**, as it signs the credentials: the
token-signing key is in `/etc/dmd-signer/oauth-key.pem`, readable by the signer only; the API
has only its public half (`/etc/dmd-gateway/oauth-public.pem`), to check tokens. Before it
signs, the signer checks the agent's signed request itself (through its read-only database
role, which may read the DID-LLMs for this), uses each one once, and signs tokens only for this
MCP server. So even a compromised API server cannot make a token for a DID-LLM that did not
just sign in itself, nor for another service.

The update (step 8) makes the key once and sets everything up, **with sign-in closed**. It
opens only once `DMD_GW_AUTH_HOST` is set, after Preska's go:

1. **Cloudflare, DNS:** add a record `auth`, the same type and target as `api`, **Proxied**
   (orange cloud).
2. **Certificate:** the origin certificate must cover `auth.debitmydata.io`:

   ```bash
   sudo openssl x509 -in /etc/ssl/cloudflare/origin.pem -noout -ext subjectAltName
   ```

   If neither `auth.debitmydata.io` nor `*.debitmydata.io` is listed, create a new origin
   certificate in Cloudflare (SSL/TLS, Origin Server) for every name listed plus
   `auth.debitmydata.io` (RSA, 15 years), and replace both files, keeping the old ones until
   the check below passes:

   ```bash
   cd /etc/ssl/cloudflare
   sudo cp origin.pem origin.pem.old && sudo cp origin.key origin.key.old
   sudo nano origin.pem.new   # paste the Origin Certificate
   sudo nano origin.key.new   # paste the Private Key
   sudo chmod 600 origin.key.new origin.key.old
   sudo openssl x509 -in origin.pem.new -noout -ext subjectAltName
   [ "$(sudo openssl x509 -in origin.pem.new -noout -pubkey | sha256sum)" = "$(sudo openssl pkey -in origin.key.new -pubout | sha256sum)" ] && echo "key matches"
   sudo mv origin.pem.new origin.pem && sudo mv origin.key.new origin.key && sudo nginx -t && sudo systemctl reload nginx
   ```

   Then check that `https://api.debitmydata.io/readyz` and `https://id.debitmydata.io` still
   answer, revoke the old certificate in Cloudflare and delete the `.old` files.
3. **Setting**, then the update (step 8), which gives the signer the sign-in server's address,
   adds the name to nginx and checks that sign-in answers there:

   ```bash
   echo "DMD_GW_AUTH_HOST=auth.debitmydata.io" | sudo tee -a /etc/dmd-gateway/gateway.env
   ```

4. **Check** from your computer:
   - `https://auth.debitmydata.io/.well-known/oauth-authorization-server`: the issuer
     `https://auth.debitmydata.io`; `/.well-known/jwks.json` there: one RSA key;
   - `https://api.debitmydata.io/readyz`: `"token_signer": "ok"`;
   - `https://api.debitmydata.io/.well-known/oauth-protected-resource/agenticsuperhighway`
     names the sign-in server, and `https://api.debitmydata.io/.well-known/mcp` lists
     `"authentication": ["oauth2.1"]`.
5. **Sign in, end to end,** with a DID-LLM made with your own key (owner-key tool,
   [owner-key.md](owner-key.md#sign-in-as-the-did-llm-oauth)):

   ```bash
   python3 did-owner-key.py whoami ~/did-keys did:web:api.debitmydata.io:agents:...
   ```

   It prints `The Agentic Superhighway says: signed in as did:web:...`.

Each token is in the audit trail (`oauth.token_issued`), and the signer logs each one it signs
or refuses (`journalctl -u dmd-gateway-signer`). To close sign-in again, remove the
`DMD_GW_AUTH_HOST` line and run the update: tokens already given stop working at once.
**Never copy the token key** off the server or into a chat; a new key (delete it and run the
update) only means agents sign in again.

## 15. Go-live checklist

- [ ] Steps 1 to 5 done; the readiness check, the issuer DID document and the policies answer
- [ ] Alerts arrive (test message), the daily report arrives, the outside uptime check is set
- [ ] A backup ran and the restore test worked; the backup key is in the password manager
- [ ] DMD payments on (step 6); the first quote's address and the monitor's line match the
      treasury record sheet
- [ ] USDC payments on (step 7) once the Coinbase key is set; Bazaar validation passes
- [ ] The temporary `/policies` redirect to staging is removed in Cloudflare
- [ ] The Agentic Spaces site points agents to `https://api.debitmydata.io`
- [ ] The Get a DID app answers at `https://id.debitmydata.io` (step 12)
- [ ] The Agentic Superhighway is listed in the official MCP Registry (step 13)
- [ ] Sign-in answers at `https://auth.debitmydata.io` (step 14), once Preska says
- [ ] Preska is told it is live
