# Monitoring, alerts and the daily report

`dmd-gateway-monitor` watches the gateway and tells people when something needs them. It runs
on the gateway server, next to the API and the payment worker, from two systemd timers that
`deploy/staging/setup.sh` installs:

| Timer | Runs | What it does |
| --- | --- | --- |
| `dmd-gateway-monitor.timer` | every 2 minutes | `dmd-gateway-monitor check`: health checks, alerts |
| `dmd-gateway-report.timer` | every day at 07:00 UTC | `dmd-gateway-monitor report`: the reconciliation report |

Alerts and reports go by email and to a Discord channel. Telegram and Slack are not used.

## 1. Settings

In `/etc/dmd-gateway/gateway.env` on the server (then nothing to restart: each run reads it
again). The real addresses and IDs live only there, never in this repository. Two rules, because
the file is read both by systemd and by the update script:

- write each value alone on its line: a comment after a value becomes part of it;
- put a value that contains spaces, `<`, `>`, `$`, `"` or other symbols (the sender name, a
  password) in single quotes: `'...'`.

```bash
# Who receives every alert and its resolution / critical alerts only / the daily report
# (comma-separated).
DMD_GW_ALERT_EMAIL_TO=ops@example.com
DMD_GW_ALERT_EMAIL_CRITICAL_TO=manager@example.com
DMD_GW_REPORT_EMAIL_TO=ops@example.com,manager@example.com

# The mailbox that sends them (SMTP with TLS: 587 with starttls, or 465 with ssl).
DMD_GW_ALERT_SMTP_HOST=smtp.example.com
DMD_GW_ALERT_SMTP_PORT=587
DMD_GW_ALERT_SMTP_SECURITY=starttls
DMD_GW_ALERT_SMTP_USERNAME=alerts@example.com
DMD_GW_ALERT_SMTP_FROM='DebitMyData Gateway <alerts@example.com>'
# Secret: this file only (mode 600).
DMD_ALERT_SMTP_PASSWORD='...'

# Discord: the webhook of a private channel (secret), and who to mention for critical alerts.
DMD_ALERT_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DMD_GW_ALERT_DISCORD_MENTION_USER_ID=123456789012345678
```

A wrong value here never stops the API or the payment worker: the monitor names the setting
in its log and in `dmd-gateway-monitor test`, and sends nothing until it is fixed.

- **Email**: any mailbox that allows SMTP with TLS works (the company's email provider with
  an app password, or an email-sending service). `DMD_GW_ALERT_SMTP_USERNAME` and
  `DMD_ALERT_SMTP_PASSWORD` are the SMTP login and its password (Mailgun: the domain's
  *SMTP credentials*); the sender in `DMD_GW_ALERT_SMTP_FROM` should be on that same domain. Vultr blocks outgoing port 25, so use 587
  or 465. Use a dedicated sender (an `alerts@` address on your own domain) and add it to the
  domain's SPF and DKIM records so the messages are not marked as spam.
- **Discord**: create a private server or channel (for example `#gateway-alerts`), then
  *Edit channel › Integrations › Webhooks › New webhook › Copy webhook URL*. Anyone with that
  URL can post to the channel, so it is a secret. Critical alerts mention the user whose ID
  is set (Discord: *Settings › Advanced › Developer mode*, then right-click your name ›
  *Copy user ID*); nobody else is ever mentioned.
- Without any channel, alerts are only written to the log (`journalctl -u dmd-gateway-monitor`).

Check the file, then send a test message the way the timers run the monitor (as root):

```bash
bash -n /etc/dmd-gateway/gateway.env && echo "file OK"
systemd-run --wait --pipe --quiet -p User=gateway -p WorkingDirectory=/opt/agent-gateway \
    -p EnvironmentFile=/etc/dmd-gateway/gateway.env \
    /opt/agent-gateway/.venv/bin/dmd-gateway-monitor test
```

It prints `Test message sent (email, discord)` when both channels are set up; the message
should arrive in the inbox and in the Discord channel. A wrong setting is named instead.
Then run the checks once and look at what they did:

```bash
systemctl start dmd-gateway-monitor.service
journalctl -u dmd-gateway-monitor -n 30 --no-pager
```

The first run records the receiving destinations in use and sends whatever is wrong at that
moment. `systemctl start dmd-gateway-report.service` sends a daily report right away.

## 2. How alerts behave

- **Critical** alerts go to everyone in `ALERT_EMAIL_TO` and `ALERT_EMAIL_CRITICAL_TO`, and to
  Discord with a mention. **Warnings** go to `ALERT_EMAIL_TO` and Discord.
- An ongoing problem is sent when it starts, again every hour while it lasts (warnings: every
  24 hours), and once more when it is resolved. Something that happened once (a refund due,
  a reorganized payment) is sent once.
- Everything found in one run is grouped into one message.
- If no channel delivers a message, it is sent again on the next run.
- Texts never contain secrets: web addresses are cut down to their host.

## 3. The alerts

| Alert | Level | What it means and what to do |
| --- | --- | --- |
| The gateway API is not ready / not answering | critical | Agents cannot get quotes or register. `systemctl status dmd-gateway-api`; `curl http://127.0.0.1:8000/readyz` shows which part fails. |
| The payment worker has stopped | critical | Payments are not verified, so nothing is issued after payment. `systemctl status dmd-gateway-worker`, `journalctl -u dmd-gateway-worker`. |
| The payment worker fails on some payments | warning | Some payments raise errors; see the worker's log. |
| USDC/DMD payments cannot be verified | critical | The DMD node, the Base RPC or the x402 facilitator does not answer or is on the wrong chain. Payments with that option are not confirmed until it is fixed. To stop new quotes for it, set its kill switch (e.g. `DMD_RAIL_X402_USDC_BASE_KILL=true`) and restart the API. |
| The DMD price is not being updated | warning | DMD quotes are refused until it is. Check the worker and the DMD node or explorer. |
| The DMD price parameters are due for their quarterly review | warning | Review the electricity price and machine efficiency in `config/payment_routes.yaml`. |
| The gateway's DMD node is N blocks behind / ahead of the explorer | warning | The node is syncing, stuck, or on another chain than the public explorer. |
| A DID's / donation's payment left the chain after it was issued | critical | A chain reorganization removed the payment. The worker keeps watching it; if it does not come back, the DID or receipt was given without payment. |
| A payment needs a refund | warning | An overpayment, a late payment or a second payment arrived. Refund it as the refund policy says. |
| USDC payments with an unknown settlement outcome | warning | Sent to the facilitator more than 30 minutes ago and still not found on chain. |
| Payments confirming for more than 2 hours | warning | Seen on chain but not confirmed enough yet. |
| A receiving destination changed | critical | A USDC receiving address, the donation address or the DMD wallet changed since the last run. Every change needs written approval: if this one was not approved, switch that payment option off with its kill switch at once and check who changed the server. |
| A new receiving destination is in use | warning | A payment option was switched on. |
| The gateway database is unreachable | critical | Sent at most once an hour (remembered in `/var/lib/dmd-gateway-monitor`). |
| The gateway's configuration cannot be loaded | critical | The gateway would refuse to start with it. |
| The monitor could not run its … check | warning | That check failed by itself; the alerts it raised before stay open. |

## 4. The daily report

Every day at 07:00 UTC, to `REPORT_EMAIL_TO` and Discord:

- **Last 24 hours**: quotes, DIDs issued, donations, payments settled per payment option with
  the amounts received, payments that failed or need a refund.
- **Since the start**: DIDs issued and amounts received per payment option.
- **Receiving destinations in use**: the USDC addresses, and the DMD wallet shown by its key
  origin and a short fingerprint (never its public key, which would reveal all its addresses).
  Anyone receiving the report can see at once if one of them changed.
- **Reconciliation**: the database checked against itself (every settled payment fully
  credited; every DID and receipt backed by a settled payment), and every payment settled in
  the last 30 days checked again on its chain. Any difference is also sent as a critical
  alert.
- **Open alerts**.

## 5. When the whole server is down

The monitor runs on the gateway server, so it cannot report that the server itself is down.
Add an outside uptime check on `https://api.debitmydata.io/readyz` (any uptime-monitoring
service; alert by email when it does not answer `"ready"`).
