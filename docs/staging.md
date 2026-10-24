# Staging server

The staging server runs the whole gateway on one VPS at `https://staging-api.debitmydata.io`,
on test networks whose coins have no value: a **private DMD regtest chain**, and **Base
Sepolia** for USDC through x402. It exists for demos and
end-to-end tests. Production (`api.debitmydata.io`) will be a separate server, set up after
the security review.

```
Internet ── Cloudflare proxy (TLS, WAF) ──► VPS :443, open to Cloudflare only
                                             nginx (Cloudflare origin certificate)
                                               └► gateway API + MCP (127.0.0.1:8000)
                                                    ├► payment worker
                                                    ├► PostgreSQL 16 (local)
                                                    ├► DMD node, regtest (RPC on 127.0.0.1 only)
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

**DMD Core:** copy your working `debitmydatad` and `debitmydata-cli` to `/usr/local/bin/` on
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
| DMD node settings / data | `/etc/debitmydata/debitmydata.conf` / `/var/lib/debitmydata` |
| Services | `debitmydata`, `dmd-gateway-api`, `dmd-gateway-worker`, `nginx`, `postgresql` |
| Logs | `journalctl -u dmd-gateway-api -f` (same for the others) |
| DMD command line | `dmd-cli ...` (for example `dmd-cli getblockchaininfo`) |

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
| `signature` (short, about 86 characters) | step 6 | step 7 |
| `payment_signature` (long, starts with `eyJ`; USDC only) | step 3 | step 8, `PAYMENT-SIGNATURE` field |

The two IDs of a quote start alike after their prefix, because both carry the time they were
made. A payment ID that is really the registration ID with its start changed is answered with
the right payment ID, and each signature sent in the other one's place is refused with a
message saying which one it is.

Three endpoints in the **staging** section stand in for what a real agent does with its own
key and wallet:

| Endpoint | Stands in for |
| --- | --- |
| `POST /v1/staging/test-agent-keys` | the agent creating its own Ed25519 key |
| `POST /v1/staging/test-payments` | the agent paying the quote: from its DMD wallet (6 blocks are mined to confirm it), or for an x402 quote, the agent's wallet signing the USDC payment |
| `POST /v1/staging/test-signatures` | the agent signing the controller challenge with its key |

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

## How staging differs from production

| | Staging | Production (to come) |
| --- | --- | --- |
| Chain | private regtest, coins without value | DMD mainnet, after the price source decision |
| USDC (x402) | Base Sepolia test USDC, the x402.org test facilitator; payments go back to the test wallet | Base mainnet USDC to DebitMyData's receiving address, a production facilitator (DEC-08) |
| Wallets on the server | test payer and treasury wallets, for demos | watch-only receiving wallet only; treasury elsewhere |
| Credential signing key | file on the server | separate signing service (ID-15) |
| DMD/USD price | fixed test price (1.00 USD) | approved price source (DEC-01) |
| Test tools on the docs page | on (`/v1/staging/...`) | never: the gateway refuses to start with them |
| Server | `staging-api.debitmydata.io` | separate server at `api.debitmydata.io` |
