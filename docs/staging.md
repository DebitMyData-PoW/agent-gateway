# Staging server

The staging server runs the whole gateway on one VPS at `https://staging-api.debitmydata.io`,
on test networks whose coins have no value: a **private DebitMyData regtest chain**, and **Base
Sepolia** for USDC through x402. It exists for demos and
end-to-end tests. Production (`api.debitmydata.io`) will be a separate server, set up after
the security review.

```
Internet ── Cloudflare proxy (TLS, WAF) ──► VPS :443, open to Cloudflare only
                                             nginx (Cloudflare origin certificate)
                                               └► gateway API + MCP (127.0.0.1:8000)
                                                    ├► payment worker
                                                    ├► PostgreSQL 16 (local)
                                                    ├► DebitMyData node, regtest (RPC on 127.0.0.1 only)
                                                    └► Base Sepolia: x402.org facilitator and
                                                       sepolia.base.org (read only), over HTTPS
```

## 1. Before you start

**VPS:** Ubuntu 24.04 LTS, 2 vCPU, 4 GB RAM. SSH on your own port, allowed in UFW, and UFW
active. The setup never changes SSH or other existing UFW rules.

**Cloudflare** (domain `debitmydata.io`):

1. DNS: an `A` record `staging-api` → the VPS address, **Proxied** (orange cloud). No other
   record may point at the VPS, so its address stays hidden.
2. SSL/TLS → Overview: mode **Full (strict)**.
3. SSL/TLS → Origin Server → **Create Certificate** for `staging-api.debitmydata.io`
   (15 years). Keep the certificate and its private key; they go on the server only.
4. Security → WAF → Custom rules: a **Skip** rule for
   `(http.host eq "staging-api.debitmydata.io")` that skips Browser Integrity Check,
   Security Level and Super Bot Fight Mode, placed first. Agents are not browsers.
5. Security → Bots: **Bot Fight Mode off** (it cannot be skipped by a rule).

**DebitMyData Core:** copy your working `debitmydatad` and `debitmydata-cli` to `/usr/local/bin/` on
the VPS and make them executable (`chmod 755`). Use the build whose regtest chain starts
correctly.

## 2. Install

As root on the VPS:

```bash
# The Cloudflare origin certificate and key (paste each, save with Ctrl+O, Enter, Ctrl+X)
mkdir -p /etc/ssl/cloudflare
nano /etc/ssl/cloudflare/origin.pem
nano /etc/ssl/cloudflare/origin.key
chmod 600 /etc/ssl/cloudflare/origin.key

# The gateway
git clone https://github.com/DebitMyData-PoW/agent-gateway.git /opt/agent-gateway
/opt/agent-gateway/deploy/staging/setup.sh
```

`setup.sh` installs nginx, PostgreSQL and Python packages, creates the `gateway` and `dmd`
service users, generates every password and the credential signing key on the server (none
is ever printed), starts the regtest node and creates its test wallets, updates the database,
starts the API and the worker, and opens port 443 to Cloudflare's address ranges only. It ends
with `{"status":"ready",...}`.

Then, from any computer: <https://staging-api.debitmydata.io/v1/discovery> and
<https://staging-api.debitmydata.io/docs>.

| What | Where |
| --- | --- |
| Settings and secrets (root only) | `/etc/dmd-gateway/gateway.env` |
| Credential signing key | `/etc/dmd-gateway/keys/issuer-key.pem` |
| DebitMyData node settings / data | `/etc/debitmydata/debitmydata.conf` / `/var/lib/debitmydata` |
| Services | `debitmydata`, `dmd-gateway-api`, `dmd-gateway-worker`, `nginx`, `postgresql` |
| Logs | `journalctl -u dmd-gateway-api -f` (same for the others) |
| DebitMyData command line | `dmd-cli ...` (for example `dmd-cli getblockchaininfo`) |

## 3. Try it from the docs page

Anyone can run the whole registration from a browser at
<https://staging-api.debitmydata.io/docs>, with nothing to install. The top of that page lists
the nine steps, each linked to its endpoint (it opens in a new tab): fill in the fields, click
**Execute**, and copy the values named into the next step.

On staging, each request body is shown as fields to fill in, with a list to pick from where
the values are fixed (such as `preferred_payment_option`). The page sends them as a form, and
the gateway turns the form into the same JSON an agent sends before checking it; `metadata`
can only be sent as JSON. Production accepts JSON only.

Which value goes where:

| Value | Where it comes from | Where it goes |
| --- | --- | --- |
| `registration_intent_id` (`dmd_did_intent_...`) | step 2 | steps 5, 7 and 8 |
| `payment_intent_id` (`dmd_payment_intent_...`) | step 2 | steps 3 and 4 |
| `signature` (short: about 86 characters, or `0x` + 130 for a wallet) | step 6 | step 7 |
| `payment_signature` (long, starts with `eyJ`; USDC only) | step 3 | step 8, `PAYMENT-SIGNATURE` field |

The two IDs of a quote start alike after their prefix, because both carry the time they were
made. A payment ID that is really the registration ID with its start changed is answered with
the right payment ID, and each signature sent in the other one's place is refused with a
message saying which one it is.

Three endpoints in the **staging** section stand in for what a real agent does with its own
key and wallet:

| Endpoint | Stands in for |
| --- | --- |
| `POST /v1/staging/test-agent-keys` | the agent creating its own Ed25519 key, and its own EVM wallet (one that holds nothing: it only signs) |
| `POST /v1/staging/test-payments` | the agent paying the quote: from its DMD wallet (6 blocks are mined to confirm it), or for an x402 quote, the agent's wallet signing the USDC payment |
| `POST /v1/staging/test-signatures` | the agent signing the controller challenge with its key, or with its wallet (`personal_sign`) |

Everything else is the real API, and the gateway checks the payment on chain exactly as it
will in production. The tools only exist when `DMD_GW_STAGING_TEST_TOOLS=true` (set by
`setup.sh`): the gateway refuses to start with them in production, and the test payer only
ever sends on a regtest chain, only the exact amount of an unpaid quote, once.

Test payments come from the `payer-test` wallet. Regtest block rewards halve every 150 blocks,
so one chain holds test coins for a few hundred registrations at the 1.00 USD test price. When
a test payment answers "no spendable test coins left", start a fresh chain (DIDs already issued
stay valid):

```bash
systemctl stop dmd-gateway-worker dmd-gateway-api debitmydata
mv /var/lib/debitmydata/regtest /var/lib/debitmydata/regtest-old
/opt/agent-gateway/deploy/staging/setup.sh
```

### A wallet as the controller

A DID can be controlled by an EVM wallet, instead of the Ed25519 key or as well as it. Step 1
also returns a throwaway wallet: `controller_wallet_address` and `wallet_private_key`.

1. In step 2, paste `controller_wallet_address` as well as `controller_public_key` (or alone,
   with `controller_public_key` left empty). Leave `controller_network` on `--`: the wallet is
   then on Base Sepolia, `eip155:84532`.
2. Do steps 5 to 7 once for each controller. Step 5 gives the next one still to prove (or pick
   it in `controller_type`). In step 6, put `private_key` in the `private_key` field when the
   challenge's `controller` starts with `ed25519:`, and `wallet_private_key` when it starts
   with `eip155:`. The wallet's `signature` starts with `0x`.
3. `GET /v1/dids/registrations/{id}` lists in `still_to_prove` what is left. Once it is
   empty, step 8 issues a DID whose document lists the wallet, and whose credential names it.

Real agents sign with their own wallet, the way wallets sign text (`personal_sign`). Smart
wallets work too: Coinbase Smart Wallet and other ERC-1271 wallets, even before they are
deployed (ERC-6492). The gateway checks those on the wallet's network through
`DMD_BASE_RPC_URL`, so on staging only Base Sepolia smart wallets can be checked.

### Paying with USDC through x402 (Base Sepolia)

The same page can pay with USDC instead of DMD, through x402 the way Coinbase-compatible
agent wallets pay:

The order is **1 → 2 → 5 → 6 → 7 → 3 → 8** (step 4 is not needed), all within the quote's 15
minutes:

1. Step 1 as before; in step 2 pick `x402_usdc_base`. The quote is 25.000000 USDC on Base
   Sepolia (`eip155:84532`).
2. Steps 5 to 7 (challenge, signature, proof). On x402 the payment is taken last, only once
   the DID can be issued. Step 8 at this point, without a payment, answers **402**: its
   `PAYMENT-REQUIRED` header (also in the error body) says what to pay. This call is
   optional.
3. Step 3 (`test-payments`) with your `payment_intent_id`: the staging test wallet signs the
   payment and returns `payment_signature`, valid for 5 minutes (`valid_until`). Nothing
   moves yet. Each call signs a new one; most of the value is the same each time, since it
   spells out the same payment terms.
4. Right away, step 8 with `payment_signature` pasted into its `PAYMENT-SIGNATURE` field. The
   facilitator settles it on Base Sepolia, the gateway checks the transfer on chain, and the
   DID comes back in the same answer, with a `PAYMENT-RESPONSE` header naming the transaction
   (viewable on `https://sepolia.basescan.org/tx/<transaction>`). If it answers 202, the
   payment is still confirming: call step 8 again without the header. If it says the
   authorization has expired, run step 3 again for a new one.

An agent with its own Base Sepolia wallet pays the same 402 itself, with any x402 client.

**The test wallet.** `setup.sh` creates it once (`DMD_STAGING_BASE_PAYER_KEY` in
`/etc/dmd-gateway/gateway.env`) and prints its address. On staging it is also the receiving
address (`DMD_USDC_BASE_MERCHANT_ADDRESS`), so its test payments come back to it and its test
USDC is never used up; it needs no ETH, since the facilitator pays the network fee. Give it
25 test USDC once at <https://faucet.circle.com> (network Base Sepolia; the faucet gives 20 per
request every 2 hours, so two requests). To see its address again:

```bash
grep -E 'DMD_USDC_BASE_MERCHANT_ADDRESS' /etc/dmd-gateway/gateway.env
```

If a test payment answers that the facilitator refused it with `insufficient_funds`, the test
wallet needs test USDC.

### An inference donation (agents)

Agents donate towards DebitMyData's AI inference in an amount they choose (people keep using
the site's "Inference $" button). From the docs page, under the registration steps:

1. **staging** › `POST /v1/staging/test-donation-payments` with an `amount_usd` such as
   `5.00`: the staging test wallet signs the payment and returns `payment_signature`, valid
   for 5 minutes.
2. **donations** › `POST /v1/donations/{name}` with `name` `inference`, the same `amount_usd`,
   and `payment_signature` in the `PAYMENT-SIGNATURE` field. The answer carries the receipt
   signed by DebitMyData, and `status_url` to read it again later.

Without the `PAYMENT-SIGNATURE` field, step 2 answers **402** with what to pay. On staging,
donations go back to the test wallet (`DMD_USDC_BASE_INFERENCE_ADDRESS`, set by `setup.sh`),
so its test USDC is never used up. In production they go to the "AgenticInference Pay"
wallet.

### Checking the routes with Coinbase's Bazaar validator

Coinbase's validator calls each paid route with the example request the gateway advertises
and reports whether the x402 Bazaar could list it (no API key needed, nothing is paid):

```bash
cd /opt/agent-gateway
.venv/bin/python scripts/validate_bazaar.py https://staging-api.debitmydata.io
```

It checks the DID registration and the inference donation, and prints `Route valid: yes`
for each route that passes, and otherwise what to fix (`--route` checks only one). Production
is checked the same way before launch (X402-10).

## 4. Demo from the command line

Run as root on the VPS, one block at a time. The VPS plays both the agent and the agent's DMD
wallet; everything else goes through the public address, Cloudflare included.

```bash
cd /opt/agent-gateway
GW=https://staging-api.debitmydata.io
PY=/opt/agent-gateway/.venv/bin/python
[ -f /root/agent-key.pem ] || $PY scripts/demo_agent.py new-key /root/agent-key.pem
AGENT_KEY=$($PY scripts/demo_agent.py public-key /root/agent-key.pem)

curl -s $GW/v1/payment-options | jq
QUOTE=$(curl -s -X POST $GW/v1/dids/registration-quotes \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg key "$AGENT_KEY" '{display_name: "StagingAgent-01",
        participant_type: "autonomous_agent", controller_public_key: $key,
        preferred_payment_option: "dmd_pow_native"}')")
echo "$QUOTE" | jq
REG=$(echo "$QUOTE" | jq -r .registration_intent_id)
PAY=$(echo "$QUOTE" | jq -r .payment_intent_id)
ADDR=$(echo "$QUOTE" | jq -r .selected_payment_option.recipient)
AMOUNT=$(echo "$QUOTE" | jq -r .selected_payment_option.amount)
```

```bash
dmd-cli -rpcwallet=payer-test sendtoaddress $ADDR $AMOUNT
sleep 12
curl -s $GW/v1/payments/$PAY | jq '{status, confirmations}'
dmd-cli generatetoaddress 6 $(dmd-cli -rpcwallet=payer-test getnewaddress) > /dev/null
sleep 12
curl -s $GW/v1/payments/$PAY | jq '{status, confirmations, next_action}'
```

```bash
CHALLENGE=$(curl -s -X POST $GW/v1/dids/registrations/$REG/controller-challenges)
SIG=$(echo "$CHALLENGE" | $PY scripts/demo_agent.py sign /root/agent-key.pem)
CID=$(echo "$CHALLENGE" | jq -r .challenge_id)
curl -s -X POST $GW/v1/dids/registrations/$REG/controller-proofs \
  -H "Content-Type: application/json" \
  -d "{\"challenge_id\": \"$CID\", \"signature\": \"$SIG\"}" | jq '{status, next_action}'
curl -s -X POST $GW/v1/dids/registrations \
  -H "Content-Type: application/json" \
  -d "{\"registration_intent_id\": \"$REG\"}" > /tmp/registration.json
jq '{did, did_document_url, entitlements}' /tmp/registration.json
$PY scripts/demo_agent.py verify-credential /tmp/registration.json
```

The DID document URL printed above opens in any browser, as does the issuer key at
<https://staging-api.debitmydata.io/.well-known/did.json>.

Through MCP, the same as section 5 of [regtest-demo.md](regtest-demo.md) with
`--url $GW/mcp` added to each `demo_agent.py mcp-...` command and `dmd-cli` instead of
`debitmydata-cli`, for example:

```bash
$PY scripts/demo_agent.py mcp-tools --url $GW/mcp
$PY scripts/demo_agent.py mcp-call dmd_get_did_registration_offer --url $GW/mcp | jq .offer.commercial_price
```

Any MCP client can connect to `https://staging-api.debitmydata.io/mcp` (Streamable HTTP).

## 5. Updating

After pushing changes to GitHub:

```bash
/opt/agent-gateway/deploy/staging/update.sh
```

It pulls the latest commit and runs `setup.sh` again: packages, database schema and services
are updated; secrets, keys, wallets and data are kept. When Cloudflare changes its address
ranges (rare), run `/opt/agent-gateway/deploy/staging/cloudflare-ips.sh`.

## 6. Request limits and load testing

**Limits.** nginx counts requests per visitor address (the real one, passed on by
Cloudflare) and in total. A visitor going faster than its share gets HTTP **429** with a JSON
error (`rate_limited`) and `Retry-After`; the docs page and an agent registering stay far
below the limits.

| Requests | Per address | In total |
| --- | --- | --- |
| Reading (`GET`) | 20 a second, after a first burst of 60 | |
| Writing: quotes, challenges, proofs, registrations | 60 a minute, after a first burst of 30 | 50 a second; up to 100 more wait their turn |
| MCP (`/mcp`, every tool call) | 5 a second, after a first burst of 30 | 100 a second |
| Staging test tools (`/v1/staging/...`) | 10 a minute, after a first burst of 10 | counted with the writes |
| Open connections | 30 | |

The gateway itself allows each controller key at most **5 open quotes** (unpaid and not
expired); a sixth is refused with 429 `too_many_open_quotes` until one is paid or expires
(`DMD_GW_MAX_OPEN_QUOTES_PER_CONTROLLER`). The limits are set in
`deploy/staging/files/nginx-site.conf`; `update.sh` applies changes.

`/v1/discovery` and `/v1/payment-options` may be kept a minute by caches
(`Cache-Control: public, max-age=60`). A Cloudflare cache rule for those two paths serves
most reads without reaching the server at all.

The API runs one process per CPU core (between 2 and 4).

**Load test.** `scripts/load_test.py` measures how many requests per second the gateway
answers and how fast. Run it on the server, against the API directly (past nginx and its
limits), to measure the gateway itself:

```bash
cd /opt/agent-gateway
.venv/bin/python scripts/load_test.py --scenario mixed --concurrency 50 --duration 30
.venv/bin/python scripts/load_test.py --scenario quotes --concurrency 20 --duration 30
```

`reads` is discovery, payment options and payment status; `quotes` creates DMD quotes (one
database transaction and one new DMD address each; `--payment-option x402_usdc_base` skips
the DebitMyData node); `mixed` is 9 reads for every quote. It prints, per kind of request, how many
were answered, refused for going too fast (429) or failed, the requests per second, and the
response times (median, 95th and 99th percentile, slowest).

For reference, on a 2-core test machine running the API, PostgreSQL, the DebitMyData node and the
load test together, one API process answered about **130 quotes** or **430 reads** a
second, limited by the processors. The total of 50 writes a second is set well below that;
if the server measures fewer than about 80 quotes a second, lower `dmd_writes_total` in
the nginx site to match.

To see the limits at work, run a short test through Cloudflare from another computer:
`--base-url https://staging-api.debitmydata.io --allow-remote --duration 10 --concurrency 5`
shows 429 answers once the burst is used up. Keep such runs short: Cloudflare may take a
long, heavy run for an attack. The script refuses to run against production. Quotes it
creates are real, unpaid quotes that expire after 15 minutes; their DMD addresses stay in the
watch-only wallet, which is harmless.

## 7. Alerts

The update installs the monitor (every 2 minutes) and the daily report (07:00 UTC). They send
nothing until the email and Discord settings are in `/etc/dmd-gateway/gateway.env`: see
[monitoring.md](monitoring.md), then run `dmd-gateway-monitor test` as shown there.

## How staging differs from production

| | Staging | Production (to come) |
| --- | --- | --- |
| Chain | private regtest, coins without value | DebitMyData mainnet |
| USDC (x402) | Base Sepolia test USDC, the x402.org test facilitator; payments go back to the test wallet | Base mainnet USDC to DebitMyData's receiving address (the AgenticSpaces DID-LLM wallet), through Coinbase's CDP facilitator with DebitMyData's API key (DEC-08) |
| Wallets on the server | test payer and treasury wallets, for demos | watch-only receiving wallet only; treasury elsewhere |
| Credential signing key | file on the server | separate signing service (ID-15) |
| DMD/USD price | quotes: fixed test price (1.00 USD); `GET /v1/prices/dmd-usd`: the mainnet price (the higher of the minimum rate and the mining cost, from the explorer's difficulty) | the higher of the minimum rate and the mining cost, from the gateway's own mainnet node averaged over a week (DEC-01) |
| Test tools on the docs page | on (`/v1/staging/...`) | never: the gateway refuses to start with them |
| Request limits | nginx on the server | at the edge (Cloudflare or AWS WAF), in front of several API servers |
| Server | `staging-api.debitmydata.io` | separate server at `api.debitmydata.io` |
