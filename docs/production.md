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
is not on the page yet (agents pay it through the API). Its font, Mada, is served by the
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
settings are not production's.

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

## 11. BottyBotty protection (a product for a DID)

Agents with a DID can buy BottyBotty protection for it: a quote, a DMD payment like the DID's,
then a receipt signed by DebitMyData and the `bottybotty_protection` entitlement on the DID
for the period bought ([architecture.md](architecture.md#products-for-a-did-bottybotty-protection)).
It is **off** until switched on, and stays off in production until:

- DebitMyData has set the price and the period: they are placeholders today (10.00 USD for
  30 days) in `config/payment_routes.yaml`, under `products: bottybotty_protection:`
  (`commercial_price`, `period_days`); a change there comes with a gateway update;
- BottyBotty (Suyash's team) protects the agents that hold it, so that nothing is sold
  before it is delivered;
- the terms and refund policy cover it (`terms_version` in the same place).

Then switch it on and check it is offered:

```bash
echo "DMD_PRODUCT_BOTTYBOTTY_PROTECTION_ENABLED=true" | sudo tee -a /etc/dmd-gateway/gateway.env
sudo systemctl restart dmd-gateway-api
curl -s https://api.debitmydata.io/v1/products
```

To stop selling it, set the value to `false` and restart the API: purchases already paid
can still be completed, and entitlements already granted stay until their end. A purchase
paid for a DID that is suspended before it is completed waits until the DID is reactivated;
if the DID is revoked, the purchase stays paid without a receipt and is refunded by hand as
the refund policy says.

## 12. Go-live checklist

- [ ] Steps 1 to 5 done; the readiness check, the issuer DID document and the policies answer
- [ ] Alerts arrive (test message), the daily report arrives, the outside uptime check is set
- [ ] A backup ran and the restore test worked; the backup key is in the password manager
- [ ] DMD payments on (step 6); the first quote's address and the monitor's line match the
      treasury record sheet
- [ ] USDC payments on (step 7) once the Coinbase key is set; Bazaar validation passes
- [ ] The temporary `/policies` redirect to staging is removed in Cloudflare
- [ ] The Agentic Spaces site points agents to `https://api.debitmydata.io`
- [ ] Preska is told it is live
