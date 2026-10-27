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
- **DebitMyData Core v1.3.3:** copy the official `debitmydatad` and `debitmydata-cli` to
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

Only with a version that already runs well on staging:

```bash
/opt/agent-gateway/deploy/production/update.sh
```

It keeps every secret, key, wallet and setting, and refuses to run on a server whose
settings are not production's.

## 9. Go-live checklist

- [ ] Steps 1 to 5 done; the readiness check, the issuer DID document and the policies answer
- [ ] Alerts arrive (test message), the daily report arrives, the outside uptime check is set
- [ ] A backup ran and the restore test worked; the backup key is in the password manager
- [ ] DMD payments on (step 6); the first quote's address and the monitor's line match the
      treasury record sheet
- [ ] USDC payments on (step 7) once the Coinbase key is set; Bazaar validation passes
- [ ] The temporary `/policies` redirect to staging is removed in Cloudflare
- [ ] The Agentic Spaces site points agents to `https://api.debitmydata.io`
- [ ] Preska is told it is live
