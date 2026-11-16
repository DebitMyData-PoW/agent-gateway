# Production server

The production gateway runs at `https://api.debitmydata.io` on its own Vultr server, in
DebitMyData's account, separate from staging and from every other DebitMyData server. It
takes real payments, so it has none of staging's test material: no test tools, no test
chain, no test wallets. Every update is tried on staging first. When something goes wrong
(stopping a payment option at once, a leaked key, an attack), see
[emergencies.md](emergencies.md).

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
settings (`DMD_GW_ISSUER_PUBLIC_KEY`), which is published at `/.well-known/did.json`. Before
the signer signs a sign-in (step 14), it also checks the DID-LLM's owner against its signed
records: the credential, then each owner change its owners signed. An owner written into
the database without them signs in to nothing.

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

**e. Restore test (once now, then every 6 months),** on the Ubuntu VM, never on production.
It proves that the backups can be read with the offline key and hold what a new server needs.
The VM needs `sudo apt install age postgresql-client` and a copy of this repository. (Ubuntu
24.04's `postgresql-client` is version 16, as on the server; an older Ubuntu has an older one,
which cannot read the server's dumps: install `postgresql-client-16` from PostgreSQL's own
apt repository there.)

1. Copy the newest pair from the server (it keeps the last 7 nights in
   `/var/backups/dmd-gateway`, readable by root) with the computer you already log in to the
   server from, then to the VM as you bring other files there. The VM itself never connects
   to the server, and the files are useless without the key. On the server, the newest
   `<time>`:

   ```bash
   ls -1 /var/backups/dmd-gateway | grep db- | tail -n 1
   ```

   On your computer (Windows PowerShell, macOS or Linux; WinSCP works too), then to the VM's
   `~/restore-test`:

   ```bash
   scp -P <SSH port> "root@<server address>:/var/backups/dmd-gateway/agent-gateway-*-<time>.*.age" .
   ```

2. Run the test from the repository, and paste the key's `AGE-SECRET-KEY-1...` line from the
   password manager when it asks (it is not shown, and it is deleted when the test ends):

   ```bash
   scripts/backup-restore-test.sh ~/restore-test
   ```

   It lists the files in the settings backup (the gateway's and the signer's settings and the
   issuer key must be there), the database dump's date and the rows in each table, without a
   database and without writing anything decrypted to disk. It ends with `RESTORE TEST
   PASSED` and the command that counts the DID-LLMs on the server: the server has the same
   number, or more if DID-LLMs were registered after the backup.

3. Delete the copies (`rm -r ~/restore-test` on the VM, and on your computer), and note the
   date of the test.

The key line it shows (`Backup key: age1...`) is the backup key's public half: it is the one
in `DMD_BACKUP_AGE_RECIPIENT` on the server (`grep RECIPIENT /etc/dmd-gateway/backup.env`).

To go further, a full restore into a scratch database on the VM (it needs PostgreSQL 16 there;
delete the decrypted file after):

```bash
age -d -i dmd-gateway-backup-key.txt agent-gateway-db-<time>.dump.age > db.dump
createdb restore_test && pg_restore -d restore_test db.dump
psql -d restore_test -c "SELECT count(*) FROM identities"
```

**Restoring production** after losing the server: a new server (step 1), then put back
`/etc/dmd-gateway`, `/etc/dmd-signer` and `/etc/debitmydata` from the settings backup before
running `setup.sh`, then `pg_restore --clean -d agent_gateway` the database backup. Keeping
the same issuer key matters: every credential already issued is checked against it. (If the
issuer key itself were lost with no backup, section 16 replaces it: its public half, still in
`DMD_GW_ISSUER_PUBLIC_KEY`, becomes a retired key and keeps the credentials valid.) The
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
refuses to run here. It ends with the discovery runbook's **handshake check**, through this
server's nginx: what any agent does to find and use the Agentic Superhighway, from the
well-known files to `tools/list`, A2A and the owners' page ([staging.md, section 11](staging.md#11-the-handshake-check-the-runbooks-day-5)).
If a check fails, the update says so (`FAIL` lines, then an error): fix it before announcing
anything. The daily report runs the same check through Cloudflare, with the MCP Registry's
listing ([monitoring.md](monitoring.md#4-the-daily-report)). To run it yourself, as agents
see the server:

```bash
/opt/agent-gateway/.venv/bin/dmd-gateway-handshake --registry https://api.debitmydata.io
```

**Going back to the previous version,** when an update breaks something that a quick fix
cannot. Every release is one commit on `main` (`0.X.Y: ...`), tried on staging first, so the
version before it is one commit back. As root:

1. Find the commit to go back to (the one before the update):

   ```bash
   git -C /opt/agent-gateway log --oneline -5
   ```

2. Did the update change the database? This lists the database changes between that commit
   and now; if it lists nothing, go to step 4:

   ```bash
   git -C /opt/agent-gateway diff --stat <that commit> HEAD -- migrations/versions
   ```

3. If it lists a new file (`migrations/versions/00NN_....py`), undo that change first, with
   the version that made it. Take a backup, then read where the change starts from (its
   `down_revision`, for example `"0016"`) and go back to it:

   ```bash
   systemctl start dmd-gateway-backup
   systemctl stop dmd-gateway-monitor.timer dmd-gateway-api dmd-gateway-worker
   grep down_revision /opt/agent-gateway/migrations/versions/00NN_*.py
   (cd /opt/agent-gateway && set -a && . /etc/dmd-gateway/gateway.env && set +a &&
     runuser -u gateway -- .venv/bin/alembic downgrade 0016)
   ```

   The API and the worker stop first, so that nothing uses the database while it changes
   (agents get an error for that minute); step 4 starts them again. What only the new version
   stored (its new tables or columns) is lost; everything else stays. With several new files,
   go back to the `down_revision` of the oldest one.

4. Put that version back and install it. `setup.sh`, not `update.sh` (which would fetch the
   newest version again), installs what is in the folder, starts or restarts every service and
   the monitor, and ends with the handshake check:

   ```bash
   git -C /opt/agent-gateway reset --hard <that commit>
   /opt/agent-gateway/deploy/production/setup.sh
   ```

5. Once the fix is on GitHub and runs well on staging, `update.sh` as usual: it moves forward
   from there.

**Security fixes in the libraries the gateway uses.** An update installs the libraries only
as far as `pyproject.toml` asks: a library already on the server in a version that satisfies
it stays as it is. So when a library publishes a security fix, the gateway raises its minimum
version there (0.55.2 did it for PyJWT, cryptography and python-dotenv), and the next update
installs the fixed version. The repository's CI audits the libraries a new install gets with
`pip-audit` (PyPI's advisory database) on every push and every Monday: a failed `audit` job
names the library and the version with the fix.

To audit what a server actually runs, from your own computer (nothing is installed on the
server; `SERVER` is how you log in to it, with `-p` for its SSH port):

```bash
ssh SERVER "/opt/agent-gateway/.venv/bin/pip freeze --exclude-editable" \
  | grep -v '^dmd-agent-gateway' > gateway-packages.txt
python3 -m venv ~/pip-audit && ~/pip-audit/bin/pip install -q pip-audit
~/pip-audit/bin/pip-audit -r gateway-packages.txt --no-deps --disable-pip
```

`No known vulnerabilities found` is the answer to expect. Otherwise each line names a library,
its version on the server and the version with the fix: a gateway release that raises that
minimum fixes it at the next update. The list holds library names and versions only, no
secret.

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
or its holder asks, change its status on the server with `dmd-gateway-admin`. On the public API,
only the DID-LLM's own owner can, and only to revoke it for good, with a message signed with its
owner key or wallet ([owner-key.md](owner-key.md#revoke-the-did-llm-for-good)); the alert then
says "by its owner". An owner can also move the DID-LLM to new owners (a new key or wallet,
signed by a current owner and each new one): each change is alerted too ("A DID-LLM changed
owners"), so that someone sees a key change that may follow a stolen key. The [DID policy](https://api.debitmydata.io/policies/did) says when DebitMyData may.

```bash
sudo dmd-gateway-admin did show <DID>
sudo dmd-gateway-admin did suspend <DID> --reason "Reported impersonation, under review" --by "Tyler"
sudo dmd-gateway-admin did reactivate <DID> --reason "Review closed: no misuse" --by "Tyler"
sudo dmd-gateway-admin did revoke <DID> --reason "Fraud confirmed" --by "Tyler"
```

`<DID>` can be the DID (`did:web:api.debitmydata.io:agents:...`), its `did.json` address, its
identity ID (`dmd_identity_...`) or its registration ID (`dmd_did_intent_...`).

`sudo dmd-gateway-admin owners check` (read-only) checks every active or suspended DID-LLM's
owners as the signer does before it signs a sign-in: proven by the DID-LLM's credential and the
owner changes its owners signed. A DID-LLM it lists cannot sign in (agents or apps) until that
is understood: an owner in the database that its signed records do not name is what a
tampered database would look like.

- **Suspend** is temporary: the DID document answers **410 Gone**, the DID's entitlements
  and credential show as suspended, until it is reactivated.
- **Revoke** is permanent: it asks you to type the end of the identity ID to confirm, and
  revokes the credential and the entitlements too. A revoked DID never comes back.
- Every change is written to the audit trail with the reason and the name given in `--by`
  (the database refuses to edit or delete it), and the monitor sends an alert within two
  minutes. `did show` prints the DID's whole history. Never put secrets or personal data in a
  reason: it is kept for good and sent in the alert.
- The registration status and `debitmydata_get_registration_status` (MCP) show the DID's status in
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

What search engines, AI assistants and agents read (`api/site_files.py`). Each name has a
`robots.txt` that welcomes them, with Content Signals (`search=yes, ai-input=yes, ai-train=yes`:
being known to agents and the models behind them is what a DID-LLM is for) and its sitemap
(`/sitemap.xml`, the pages at that name), and `/.well-known/api-catalog` (RFC 9727) lists the
REST API and the MCP server. The page itself describes the DID-LLM for them: schema.org
structured data (the DID-LLM as a Product with its price and image, DebitMyData, the site),
Open Graph tags for link previews, a `Link` header to the API catalog, the OpenAPI description
and `llms.txt`, and the same text in Markdown for an agent that asks for it
(`Accept: text/markdown`). Staging's `robots.txt` asks crawlers to stay away, with no sitemap.

The price is one text, word for word, wherever an agent looks: `$25.00 USD, paid once` on the
page, in `llms.txt`, in `/v1/discovery` and in the MCP tools, all from the configuration
(`config/payment_routes.yaml`). Agent checkers such as Axidort look for the price an agent found
through the API or the MCP server on the product's page: the tools now give that page
(`product_url`), and the page shows the same text. Agents that use the MCP server get the
product (name, price, page, picture) before any tool call, in the server's instructions, and
first from `debitmydata_discover` and `debitmydata_get_did_registration_offer`.
`debitmydata_open_checkout` opens the checkout without buying anything (read-only: the checkout
page and the agent's steps), so an agent that may not pay, such as a checker's, can still reach
the purchase step; and the page at the MCP server's address says what is sold there, with the
same price text.

Agents that use Agent Skills find one at `/.well-known/agent-skills/index.json`:
`debitmydata-did-llm`, a SKILL.md the gateway writes itself from llms.txt, so it always has
this server's addresses, price and payment options. To change its words, edit
`src/dmd_gateway/api/agent_skills.py` (the top part) or `llms.py` (the rest); the index's
digest follows by itself.

Cloudflare's **Bot Preference Sync** (Security settings, Bot traffic, "AI Crawler and Agent
access policies") must stay off for debitmydata.io: it puts Cloudflare's own lines in front of
the gateway's `robots.txt`, which say `ai-train=no` and turn away AI crawlers such as GPTBot and
ClaudeBot. Check from your computer:

```bash
curl -s https://id.debitmydata.io/robots.txt | head -n 3
```

The first line should be `# DebitMyData: search engines, AI assistants and agents are welcome.`

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

A2A agents (Agent2Agent protocol) find the same service through its **Agent Card**, which the
AI Catalog lists too; its endpoint shares the MCP server's rate limits (it is under
`/agenticsuperhighway`). Check after an update:

```bash
curl -s https://api.debitmydata.io/.well-known/agent-card.json | jq '.supportedInterfaces'
curl -s https://api.debitmydata.io/agenticsuperhighway/a2a -H 'content-type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"1","role":"ROLE_USER","parts":[{"data":{"skill":"debitmydata_get_did_registration_offer"}}]}}}' \
  | jq '.result.message.parts[1].data.offer.price_text'
```

The first shows the A2A endpoint twice (versions 1.0 and 0.3), the second `"$25.00 USD, paid
once"`. The handshake check that ends every update does the same (its `a2a:` lines).

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

Agents whose DID-LLM is owned by a **wallet** sign in with it instead (the runbook's
wallet-signature path: a message from `https://auth.debitmydata.io/challenge`, signed with the
wallet, for the same token; [staging.md, section 9](staging.md#9-oauth-phase-1-the-authorization-server)).
The signer checks that message and the wallet's signature itself too, with no network: an
ordinary wallet can sign in, a smart wallet not yet.

People can also sign in to **apps** (MCP clients such as ChatGPT or Claude) with their
DID-LLM, in the browser; how it works:
[staging.md, section 10](staging.md#10-oauth-phase-2-people-sign-in-to-apps). Here too the
signer checks each sign-in itself: the message the person signed (for this sign-in server,
this DID-LLM, that app's site, that scope and this MCP server, still valid, used once) and the
signature, with the DID-LLM's current owner key or ordinary wallet. Only then does it give a
**grant** for the sign-in, valid 30 days, which the API keeps (never the app); every access
token for that app is signed against it, for the same app, MCP server and scope. So a
compromised API cannot get tokens for a DID-LLM whose owner did not sign in to that app, and a
sign-in lasts 30 days at most (then the app asks the person to sign in again). The signer has
no network, so it cannot ask a smart wallet's chain: **smart wallets cannot sign in to apps in
production yet** (the page says so, and offers the owner key when the DID-LLM has one);
ordinary wallets and keys can.

The update (step 8) makes the key once and sets everything up, **with sign-in closed**. It
opens only once `DMD_GW_AUTH_HOST` is set, after Preska's go:

1. **Cloudflare, DNS:** add a record `auth`, the same type and target as `api`, **Proxied**
   (orange cloud), and add `auth.debitmydata.io` to the WAF Skip rule that `api` has
   (Security → WAF → Custom rules: add `or (http.host eq "auth.debitmydata.io")` to its
   expression and keep everything it already has; it skips Browser Integrity Check, Security
   Level and Super Bot Fight Mode). Apps' servers fetch the metadata and call `/token`
   themselves: without it Cloudflare answers them `403`, error **1010**.
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
     `https://auth.debitmydata.io` and `"authorization_endpoint":
     "https://auth.debitmydata.io/authorize"`; `/.well-known/jwks.json` there: one RSA key;
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
6. **An app, end to end:** in Claude (a custom connector) or ChatGPT (developer mode), add
   the MCP server `https://api.debitmydata.io/agenticsuperhighway`, ask it to run
   `debitmydata_whoami`, sign in on the page it opens with your DID-LLM (wallet or key), and run it
   again: it answers with your DID-LLM. If the app cannot reach the server or sign in, look at
   Cloudflare's Security Events for `api` and `auth`: the apps' own servers (Anthropic's
   `160.79.104.0/21`, OpenAI's) must get through, as agents do.

Each token is in the audit trail (`oauth.token_issued`), each person's sign-in too
(`oauth.sign_in_approved`, `oauth.sign_in_rejected`), and the signer logs each token and grant
it signs or refuses (`journalctl -u dmd-gateway-signer`). To close sign-in again, remove the
`DMD_GW_AUTH_HOST` line and run the update: tokens already given stop working at once, for
agents and apps. **Never copy the token key** off the server or into a chat; a new key (delete
it and run the update) only means agents and people sign in again.

**The identity endpoint** (`https://api.debitmydata.io/agenticsuperhighway/identity`, since
0.49.0) answers while sign-in is open, with nothing to set: a separate MCP server with three
read-only tools (`debitmydata_whoami`, `debitmydata_get_profile`, `debitmydata_verify_agent`),
each for a signed-in DID-LLM, with tokens of its own
([integrations.md](integrations.md#6-the-access-token-the-mcp-server-and-checking-an-agent)). To
check it after the update:

```bash
curl -s https://api.debitmydata.io/.well-known/oauth-protected-resource/agenticsuperhighway/identity | jq .resource
```

It prints the endpoint's address. Ending an app's sign-in (`/revoke`) now ends its access
tokens at once too.

**Google Connected Apps (Gemini, Search):** Google checks that the MCP server's site is ours
with a file its console gives (once DebitMyData has access to the console). Put it on the
server and name it in the settings; the gateway serves it at
`https://api.debitmydata.io/.well-known/google-connected-apps`, read at each request:

```bash
sudo install -m 644 -o root -g root google-connected-apps.json /etc/dmd-gateway/google-connected-apps.json
echo "DMD_GW_GOOGLE_CONNECTED_APPS_FILE=/etc/dmd-gateway/google-connected-apps.json" | sudo tee -a /etc/dmd-gateway/gateway.env
sudo systemctl restart dmd-gateway-api
curl -s https://api.debitmydata.io/.well-known/google-connected-apps
```

The last command prints the file. Google's servers call the MCP server and the token endpoint
themselves: if linking fails, look at Cloudflare's Security Events for `api` and `auth`, as for
Claude and ChatGPT above.

Google's apps register (RFC 7591) before they sign anyone in, which is off until the settings
switch it on (0.52.0; test it on staging first, [staging.md](staging.md) section 10). Switch it
on when the app is being set up in Google's console:

```bash
echo "DMD_GW_OAUTH_REGISTRATION_ENABLED=true" | sudo tee -a /etc/dmd-gateway/gateway.env
sudo systemctl restart dmd-gateway-api
curl -s https://auth.debitmydata.io/.well-known/oauth-authorization-server | jq .registration_endpoint
```

The last command prints `"https://auth.debitmydata.io/register"`. In Google's console, the MCP
server's address is the identity endpoint's,
`https://api.debitmydata.io/agenticsuperhighway/identity`: Google's apps get tokens for it
only.

## 15. Go-live checklist

- [ ] Steps 1 to 5 done; the readiness check, the issuer DID document and the policies answer
- [ ] Alerts arrive (test message), the daily report arrives, the outside uptime check is set
- [ ] A backup ran and the restore test worked; the backup key is in the password manager
- [ ] The firewall, checked from outside, lets only SSH answer
      ([emergencies.md, part 8](emergencies.md#8-the-firewall-checked-from-outside))
- [ ] Whoever looks after the server has read [emergencies.md](emergencies.md), and has
      tried a kill switch on staging
- [ ] DMD payments on (step 6); the first quote's address and the monitor's line match the
      treasury record sheet
- [ ] USDC payments on (step 7) once the Coinbase key is set; Bazaar validation passes
- [ ] The temporary `/policies` redirect to staging is removed in Cloudflare
- [ ] The Agentic Spaces site points agents to `https://api.debitmydata.io`
- [ ] The Get a DID app answers at `https://id.debitmydata.io` (step 12)
- [ ] The Agentic Superhighway is listed in the official MCP Registry (step 13)
- [ ] The handshake check passes through Cloudflare, with the Registry (step 8)
- [ ] Sign-in answers at `https://auth.debitmydata.io` (step 14), once Preska says
- [ ] Preska is told it is live

## 16. Replacing the issuer key

The issuer key signs every DID-LLM credential and receipt. Replace it on a schedule you choose
(for example once a year), when someone who had root access to the server leaves, or if its
private key file was lost. The credentials and receipts signed so far stay valid: the old
**public** key stays listed in the issuer's DID document as a **retired key**, where anyone can
still verify what it signed, and the signer still trusts the DID-LLM credentials it signed.
Nothing new is ever signed with a retired key.

**If the old private key may have leaked** (copied off the server, in a backup someone else
could decrypt), do not list it as retired: whoever has it could sign credentials that verify.
That is an incident: tell Preska first; what to do with the credentials it signed is decided
with her.

As root on the production server:

1. Check that last night's backup ran (step 4): it holds the current key.
2. Note the current public key; it becomes the retired key:

   ```bash
   grep '^DMD_GW_ISSUER_PUBLIC_KEY=' /etc/dmd-gateway/gateway.env
   ```

3. Stop the signer, put the old key file aside (not deleted yet) and create the new key. The
   last command prints the new public key (`Public key: z6Mk...`):

   ```bash
   systemctl stop dmd-gateway-signer
   mv /etc/dmd-signer/issuer-key.pem /etc/dmd-signer/issuer-key.retired-$(date -u +%Y%m%d).pem
   runuser -u dmd_signer -- /opt/agent-gateway/.venv/bin/dmd-gateway-issuer-key /etc/dmd-signer/issuer-key.pem
   ```

   While the signer is stopped, a DID-LLM cannot be issued (the agent is told to try again in
   a minute; nothing is lost) and nobody signs in.

4. In `/etc/dmd-gateway/gateway.env` (`nano`), set `DMD_GW_ISSUER_PUBLIC_KEY` to the **new**
   public key, and add the **old** one as a retired key. If the line already exists (an earlier
   replacement), put the old key first, then a comma, then the keys already there:

   ```bash
   DMD_GW_ISSUER_PUBLIC_KEY=z6Mk<new>
   DMD_GW_ISSUER_RETIRED_KEYS=z6Mk<old>
   ```

5. Run the setup, which checks both, gives the retired keys to the signer and restarts
   everything:

   ```bash
   /opt/agent-gateway/deploy/production/setup.sh
   ```

   If it stops with "does not match DMD_GW_ISSUER_PUBLIC_KEY", step 4 is not saved yet; with
   "listed in DMD_GW_ISSUER_RETIRED_KEYS", the new key was put in the retired list by mistake.

6. Check: the DID document lists the new key first, then the old one; the readiness check
   says the signer is ready; an existing DID-LLM's credential still verifies (`true`):

   ```bash
   curl -s https://api.debitmydata.io/.well-known/did.json | jq '.assertionMethod'
   curl -s https://api.debitmydata.io/readyz | jq '.checks.credential_signer'
   curl -s -X POST https://api.debitmydata.io/v1/dids/verifications \
     -H 'content-type: application/json' -d '{"did": "<an existing DID-LLM>"}' \
     | jq '.credential.signature_valid'
   dmd-gateway-admin owners check
   ```

   The last command checks every DID-LLM's owners as the signer does before a sign-in, with
   the keys now in the settings: "every owner is proven by its signed records". If it lists
   DID-LLMs whose credential is "not one DebitMyData signed for it", the old key is missing
   from `DMD_GW_ISSUER_RETIRED_KEYS`.

7. Then destroy the old private key file. It stays in the encrypted backups until they expire
   (`DMD_BACKUP_KEEP_DAYS`, 30 days):

   ```bash
   shred -u /etc/dmd-signer/issuer-key.retired-*.pem
   ```

Staging works the same way with its key file (`DMD_GW_ISSUER_KEY_PATH`): note the old public
key (the first `publicKeyMultibase` in staging's `/.well-known/did.json`), move the key file
aside, create a new one at the same place with `dmd-gateway-issuer-key` (as the `gateway`
user), add the old public key to `DMD_GW_ISSUER_RETIRED_KEYS`, then restart the API and the
worker.
