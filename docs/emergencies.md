# Emergencies (production)

What to do when something goes wrong on the production gateway, the fastest measures first.
Each part says when it applies, what to do, how to check it worked, and how to undo it.
Commands run as root on the production server unless a part says otherwise. Write down what
you do and when (UTC) as you go: part 9 needs it.

| Situation | Part |
| --- | --- |
| One payment option misbehaves (wrong amounts, a provider incident, a suspicious payment) | [1](#1-stop-one-payment-option-the-kill-switch) |
| Every sale must stop now | [2](#2-stop-every-sale) |
| The DebitMyData node or chain looks wrong (a reorganization alert, a different chain from the explorer) | [3](#3-the-debitmydata-node-or-chain-isolation) |
| Too many requests, an attack on the site | [4](#4-too-many-requests-an-attack-on-the-site) |
| A secret or key may have leaked | [5](#5-a-secret-or-a-key-may-have-leaked) |
| The server itself may be compromised | [6](#6-the-server-may-be-compromised) |
| The server is down | [7](#7-the-server-is-down) |
| The firewall, checked from outside | [8](#8-the-firewall-checked-from-outside) |
| Afterwards | [9](#9-afterwards) |

## 1. Stop one payment option (the kill switch)

Each payment option has a kill switch in `/etc/dmd-gateway/gateway.env`:

| Payment option | The line that stops it |
| --- | --- |
| PayPal | `DMD_RAIL_PAYPAL_KILL=true` |
| Card (Stripe) | `DMD_RAIL_CARD_STRIPE_KILL=true` |
| DebitMyData coin (DMD) | `DMD_RAIL_DMD_POW_NATIVE_KILL=true` |
| USDC on Base (x402) | `DMD_RAIL_X402_USDC_BASE_KILL=true` |
| Wrapped DMD on Base (not live) | `DMD_WRAPPED_DMD_KILL=true` |
| USDC on Solana (not live) | `DMD_SOLANA_PAYMENTS_KILL=true` |

**Do:**

1. `nano /etc/dmd-gateway/gateway.env`, and add the line (or, if it is there with a `#` in
   front, remove the `#`). Save.
2. Restart the API and the worker:

   ```bash
   systemctl restart dmd-gateway-api dmd-gateway-worker
   ```

**Check:** the API's start line names it among the stopped options, and the payment options
agents see no longer list it:

```bash
journalctl -u dmd-gateway-api -n 50 --no-pager | grep -o '"killed_rails": \[[^]]*\]'
curl -s 'https://api.debitmydata.io/v1/payment-options?service=did_registration' \
  | jq -r '.supported_payment_options[].id'
```

**What it does:** no new quote with that option; payments already started with it are not
checked, and no DID-LLM is issued for them, until it is switched back on. Nothing is lost:
they continue from where they stopped. Card and PayPal payment pages close by themselves when
their quotes end. Every other payment option keeps working.

**Undo,** once the cause is understood and fixed: put `#` back in front of the line (or delete
it), restart the API and the worker the same way, and check that the option is listed again.

## 2. Stop every sale

Part 1 for every payment option that is on (the `..._ENABLED=true` lines in `gateway.env`),
with one restart at the end. The site, the DID documents and the verification of existing
DID-LLMs keep working: only new purchases stop.

Stopping the API itself (`systemctl stop dmd-gateway-api`) stops all of it, verification
included; keep that for part 6.

## 3. The DebitMyData node or chain (isolation)

**When:** a "payment left the chain after it was issued" alert (a reorganization), the node on
a different block than the public explorer, or the node behaving in a way you do not
understand. On a chain with little mining power, recent blocks are cheap to rewrite: treat it
as a possible attack until shown otherwise.

The production node holds only the treasury's **watch-only** key: nothing on the server can
spend DMD. Its RPC answers on the server only (`127.0.0.1:18557`), and it takes no incoming
peers (`listen=0`).

**Do:**

1. Stop DMD payments (part 1, `DMD_RAIL_DMD_POW_NATIVE_KILL=true`).
2. Cut the node off from its peers, so that it stops following a chain that may be rewritten:

   ```bash
   dmd-cli setnetworkactive false
   dmd-cli getnetworkinfo | jq '.networkactive, .connections'    # false, 0
   ```

   If this version of the node does not know `setnetworkactive`, stop it instead:
   `systemctl stop debitmydata` (the monitor then warns that DMD payments cannot be verified,
   which is expected).
3. Check that its RPC still answers on the server only:

   ```bash
   ss -ltnp | grep ':18557'      # 127.0.0.1:18557 only, never 0.0.0.0 or a public address
   ```

4. Compare the node with the public explorer: `dmd-cli getblockcount` and
   `dmd-cli getbestblockhash`, against the same block on the explorer. Note the DID-LLMs and
   receipts whose payments the alert names (`dmd-gateway-admin did show <DID>`).

**Undo,** once the cause is understood (tell Preska what happened before switching DMD
payments back on): `dmd-cli setnetworkactive true` (or `systemctl start debitmydata`), wait
until `dmd-cli getblockcount` matches the explorer, then remove the kill switch (part 1).

What to do with DID-LLMs whose payment left the chain is decided with Preska; suspending one
is in [production.md, section 9](production.md#9-suspending-reactivating-or-revoking-a-did).

## 4. Too many requests, an attack on the site

nginx on the server already limits each address (it answers HTTP 429), and so do Cloudflare's
rate-limiting rules where they are set. When that is not enough:

1. In Cloudflare, **Security → Events**: see where the requests come from (addresses,
   networks, countries) and which paths they ask for.
2. Block the source **in Cloudflare**, not on the server: a WAF custom rule on the address,
   the network (AS number) or the path, with the action Block (or Managed Challenge for
   pages people open; agents cannot pass a challenge, so not for `/agenticsuperhighway`,
   `/v1` or `/.well-known`).
3. Last resort: Cloudflare's "Under Attack" mode. It stops automated clients too, so agents
   cannot reach the gateway while it is on: switch it off as soon as possible.

**Check:** the Events page shows the blocked requests; `curl -s
https://api.debitmydata.io/readyz` still answers `"ready"` from your own computer.

## 5. A secret or a key may have leaked

"Leaked" means it may be known to someone else: shown in a chat, a screenshot, a ticket or a
repository, copied off the server, or on a computer that was lost. Replace it at once; then
look at what it allowed (part 9).

| Secret | Where it is | What to do |
| --- | --- | --- |
| PayPal app secret (`DMD_PAYPAL_CLIENT_SECRET`) | `gateway.env` | Stop PayPal (part 1). In PayPal's developer dashboard (Apps & Credentials, the live app), create a new secret and delete the old one. Put the new one in `gateway.env` (in single quotes), restart the API and the worker, remove the kill switch. |
| Stripe key (`DMD_STRIPE_SECRET_KEY`) | `gateway.env` | Stop card payments (part 1). In Stripe's dashboard (Developers → API keys), roll the gateway's restricted key. Put the new one in `gateway.env`, restart, remove the kill switch. |
| Issuer key (signs every DID-LLM credential) | `/etc/dmd-signer/issuer-key.pem` | An incident: tell Preska first. Replace it as [production.md, section 16](production.md#16-replacing-the-issuer-key) says, **without** listing the old key as retired (whoever has it could sign credentials that verify). |
| OAuth token-signing key | `/etc/dmd-signer/oauth-key.pem` | Below this table. |
| Backup key (age, offline) | your password manager | Make a new one (production.md, step 4a), put its public key in `DMD_BACKUP_AGE_RECIPIENT` in `/etc/dmd-gateway/backup.env`, run `systemctl start dmd-gateway-backup` and check its log. The old backups stay readable with the old key: whoever also has the backup files has every secret in them, so if the storage keys may have leaked too, treat every secret in this table as leaked. |
| Object storage keys (`RCLONE_CONFIG_VULTR_...`) | `/etc/dmd-gateway/backup.env` | In Vultr, regenerate the object storage keys; put the new ones in `backup.env`; `systemctl start dmd-gateway-backup` and check its log. |
| Database password (in `DMD_GW_DATABASE_URL`) | `gateway.env` | The database answers on the server only, so it is usable only there: if it leaked from the server, see part 6. To change it, put a new value (`python3 -c 'import secrets; print(secrets.token_hex(32))'`) in place of the password in that URL, then run `/opt/agent-gateway/deploy/production/setup.sh`: it gives the new password to the database and restarts everything. |
| DebitMyData node RPC password (`DMD_RPC_PASSWORD`) | `gateway.env`, the node's settings | The node's RPC answers on the server only (`127.0.0.1`), so it is usable only there: a leak means someone may be on the server, see part 6. |
| Alert mailbox password (`DMD_ALERT_SMTP_PASSWORD`), Discord webhook (`DMD_ALERT_DISCORD_WEBHOOK_URL`) | `gateway.env` | Change the password at the mail provider, or delete the webhook in Discord (channel settings, Integrations) and make a new one; put the new value in `gateway.env`. The monitor reads it at its next run; send a test message ([monitoring.md, section 1](monitoring.md#1-settings)). Someone with the old ones could send messages that look like alerts, not read anything. |
| Coinbase (CDP) API key (`DMD_X402_CDP_API_KEY_SECRET`), a Base RPC address with a key in it (`DMD_BASE_RPC_URL`) | `gateway.env` | Stop USDC payments (part 1). Delete the key at Coinbase's Developer Platform (or the RPC provider) and make a new one; put it in `gateway.env`, restart the API and the worker, remove the kill switch. |
| Cloudflare origin certificate key | `/etc/ssl/cloudflare/origin.key` | In Cloudflare (SSL/TLS → Origin Server), revoke the certificate and create a new one; replace both files as at installation; `nginx -t && systemctl reload nginx`. |
| SSH access to the server | your computer | Remove the key from `/root/.ssh/authorized_keys` (and any other account's), add a new one; look at `last` and `journalctl -u ssh` for logins you did not make: if there are any, part 6. |
| A DID-LLM owner's key or wallet | the owner's | The owner, with another owner if there is one, moves it to a new key or wallet, or revokes it, at `https://id.debitmydata.io/manage`. DebitMyData can suspend it meanwhile ([production.md, section 9](production.md#9-suspending-reactivating-or-revoking-a-did)). |
| MCP Registry key (publishes the listing) | `~/.mcp-registry/key.pem` on the computer you publish from | Make a new key and put its DNS record in place of the old one (`v=MCPv1; ...`, [production.md, section 13](production.md#13-the-official-mcp-registry)): whoever has the old key could publish a different listing under DebitMyData's name until the record changes. Then check the listing (`dmd-gateway-handshake --registry`). |

This table is the list of the gateway's secrets, by name and place only: their values are never
written in a ticket, a chat, a document or the repository. The treasury's keys are on no
server at all (the DMD Treasury Wallet guide).

**The OAuth token-signing key.** It signs the sign-in tokens agents use with the MCP server
(valid for 10 minutes) and the 30-day sign-ins people give apps (ChatGPT, Claude). Put it
aside and let the setup make a new one; the API then knows only the new key's public half, so
everything signed with the old key stops working at once: agents sign in again, and people
sign in to their apps again (whoever has the old key can forge neither any more):

```bash
systemctl stop dmd-gateway-signer
mv /etc/dmd-signer/oauth-key.pem /root/oauth-key.replaced-$(date -u +%Y%m%d).pem
/opt/agent-gateway/deploy/production/setup.sh
curl -s https://api.debitmydata.io/readyz | jq '.checks'
```

Delete the old file once you no longer need it to look at what happened.

## 6. The server may be compromised

**When:** logins, processes, files or settings changes you did not make, or a leaked root
password or SSH key that was used.

1. Stop every sale (part 2), and tell Preska.
2. In Vultr: take a **snapshot** of the server (to look at later), then cut it off: a Vultr
   firewall group that allows only your own address, or power it off.
3. Build a new server and restore it from a backup made **before** the compromise
   ([production.md, section 4](production.md#4-backups), "Restoring production"). Replace every
   secret in part 5 on the way, as you would after a leak: the old server had them all. The
   issuer key is decided with Preska (part 5).
4. Point `api.debitmydata.io` (and `id.`, `auth.`) to the new server in Cloudflare, run the
   handshake check, and check the firewall from outside (part 8).

Never put the old server back into service, even after cleaning it.

## 7. The server is down

The outside uptime check sends the email ([monitoring.md, section 5](monitoring.md#5-when-the-whole-server-is-down)).

1. Vultr's page for the server: is it running? Its console shows the screen even when SSH
   does not answer.
2. If it runs: `systemctl status nginx dmd-gateway-api dmd-gateway-signer dmd-gateway-worker
   postgresql`, and `journalctl -u <the failed one> -n 100 --no-pager` for why.
3. If the server is lost: a new server, restored from the backups
   ([production.md, section 4](production.md#4-backups), "Restoring production").

## 8. The firewall, checked from outside

On your own computer (not the server; not through Cloudflare), with the server's **own**
address (Vultr's page for the server) and your SSH port:

```bash
scripts/firewall-check.sh <server address> <SSH port>
```

It tries the ports a server like this could expose, all at once (a few seconds). Only the SSH
port may answer: HTTPS (443) answers Cloudflare's addresses only, and the database, the
gateway API (8000) and the DebitMyData node's RPC (18557) answer nobody from outside. Then it
asks the SSH port which logins it accepts, offering none itself (it logs in to nothing):
`SSH logins: publickey (keys only, as expected)`. It ends with `FIREWALL CHECK PASSED`. Run
it again with the server's IPv6 address if it has one.

If a port is `OPEN`: on the server, `ufw status numbered` lists the rules; remove the one that
opens it with `ufw delete <number>`. Port 443 should be open to Cloudflare's ranges only
(`/opt/agent-gateway/deploy/staging/cloudflare-ips.sh` sets them again).

If SSH accepts a **password** (`publickey,password`): passwords can be guessed. First check,
in a new window of your usual SSH program, that your key logs you in without a password (so
the change cannot lock you out); then, on the server:

```bash
sshd -T | grep -E '^(passwordauthentication|permitrootlogin) '     # what SSH uses now
grep -rni 'PasswordAuthentication' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
  > /etc/ssh/sshd_config.d/00-keys-only.conf
sshd -t && systemctl reload ssh
sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '
```

The last line shows `no`, `no` and `without-password`. On Vultr's Ubuntu image the password
comes from `/etc/ssh/sshd_config.d/50-cloud-init.conf`, which wins over `sshd_config`:
SSH keeps the first value it reads, and `00-keys-only.conf` is read before it. Keep the
session open until a new key login works (Vultr's console gets you in otherwise), then run
the firewall check again.

## 9. Afterwards

- Write down what happened, when (UTC), what was done, and what is still open.
- Tell Preska, and the people affected (a payer whose payment was refused or stopped, an
  owner whose DID-LLM was suspended).
- Look at what the incident allowed: payments and DID-LLMs in that time
  (`dmd-gateway-admin did show <DID>`, the daily report), the provider's dashboard (PayPal,
  Stripe), Cloudflare's Security Events.
- Undo the temporary measures (kill switches, blocks, "Under Attack" mode) one by one, each
  with its check.
