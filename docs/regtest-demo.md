# Regtest demo: an agent pays for its DID registration in DMD and receives its DID

This walkthrough runs the whole registration flow on a private regtest chain:

1. an agent discovers DebitMyData and asks for a $25.00 DID registration quote;
2. the gateway answers with a fresh DMD address and the exact amount;
3. the agent pays;
4. the gateway detects the payment, verifies it independently, counts confirmations and
   marks it **settled** after 6 confirmations;
5. the agent proves it controls its key by signing a one-time challenge;
6. the gateway issues its DID, a signed credential (the receipt) and its entitlement.

Regtest coins have no value and the chain only exists on your machine. Never point the
demo at a mainnet node or a real wallet.

## 1. DebitMyData node (once)

Requires DebitMyData Core v1.3.3 (`debitmydatad`, `debitmydata-cli`).

`~/.debitmydata/debitmydata.conf`:

```
regtest=1
server=1
[regtest]
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
fallbackfee=0.0001
rpcauth=gateway:<value printed by share/rpcauth/rpcauth.py gateway>
```

The `rpcauth=` line holds a hash of the password, without quotes. The gateway needs the
password itself, which `rpcauth.py` prints separately.

```bash
debitmydatad -daemon

# Customer wallet with test coins (coinbase coins mature after 250 blocks)
debitmydata-cli createwallet payer-test
debitmydata-cli generatetoaddress 260 $(debitmydata-cli -rpcwallet=payer-test getnewaddress)

# Treasury (holds the keys) and the gateway's watch-only copy of its receive addresses
debitmydata-cli createwallet treasury-test
DESC=$(debitmydata-cli -rpcwallet=treasury-test listdescriptors | jq -r '.descriptors[] | select((.desc|startswith("wpkh(")) and .internal==false) | .desc')
debitmydata-cli createwallet gateway-receive true true
debitmydata-cli -rpcwallet=gateway-receive importdescriptors "[{\"desc\":\"$DESC\",\"timestamp\":\"now\",\"active\":true,\"internal\":false,\"range\":[0,1000]}]"
```

The gateway refuses to start the DMD rail if its wallet can spend funds. After a restart
of the node, load the wallets again (`debitmydata-cli loadwallet payer-test true`, and the
same for the other two; `true` makes them load automatically from then on).

## 2. Gateway settings

From the repository root, with the virtual environment active:

```bash
pip install -e ".[dev]"                          # after every update of the code
dmd-gateway-issuer-key secrets/issuer-key.pem    # once: the key that signs credentials
```

`.env` (each name once; the password is the one printed by `rpcauth.py`, without quotes):

```
DMD_GW_ROUTE_REGISTRY_PATH=config/payment_routes.regtest.yaml
DMD_RAIL_DMD_POW_NATIVE_ENABLED=true
DMD_RPC_URL=http://127.0.0.1:58557
DMD_RPC_USER=gateway
DMD_RPC_PASSWORD=<rpcauth password>
DMD_RPC_WALLET=gateway-receive
DMD_STATIC_TEST_PRICE_USD=1.00
DMD_GW_ISSUER_KEY_PATH=secrets/issuer-key.pem
```

`DMD_STATIC_TEST_PRICE_USD` is a fixed demo price (25.00 USD / 1.00 = 25 DMD per registration).
It only works outside production and never on a mainnet rail. If `sendtoaddress` answers
"Insufficient funds", mine more blocks to the payer wallet (coins mature after 250 blocks).
`.env`, `secrets/` and `*.pem` files are ignored by git.

## 3. Run it

Two terminals, each with the virtual environment active, from the repository root:

```bash
# Terminal 1: API
alembic upgrade head
uvicorn --factory dmd_gateway.main:create_app

# Terminal 2: payment worker
dmd-gateway-worker
```

The API's startup line shows `"enabled_rails": ["dmd_pow_native"]` and `"did_issuance": true`;
the worker prints `payment rail ready`. Both keep running.

## 4. The agent

Terminal 3 plays the agent. Run it from the repository root with the virtual environment
active (for `scripts/demo_agent.py`), one block at a time.

```bash
# The agent's own key: it will control the DID (the private key never leaves this file)
python scripts/demo_agent.py new-key agent-key.pem
AGENT_KEY=$(python scripts/demo_agent.py public-key agent-key.pem)

# Discover the offer
curl -s localhost:8000/v1/payment-options | jq

# Ask for a quote
QUOTE=$(curl -s -X POST localhost:8000/v1/dids/registration-quotes \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg key "$AGENT_KEY" '{display_name: "DemoAgent-01",
        participant_type: "autonomous_agent", controller_public_key: $key,
        preferred_payment_option: "dmd_pow_native"}')")
echo "$QUOTE" | jq
REG=$(echo "$QUOTE" | jq -r .registration_intent_id)
PAY=$(echo "$QUOTE" | jq -r .payment_intent_id)
ADDR=$(echo "$QUOTE" | jq -r .selected_payment_option.recipient)
AMOUNT=$(echo "$QUOTE" | jq -r .selected_payment_option.amount)
```

`new-key` refuses to overwrite an existing file; to run the demo again, skip it and only set
`AGENT_KEY`.

```bash
# Pay it, then watch: confirming -> verified -> settled (the worker checks every 10 s)
debitmydata-cli -rpcwallet=payer-test sendtoaddress $ADDR $AMOUNT
sleep 12
curl -s localhost:8000/v1/payments/$PAY | jq '{status, confirmations}'
debitmydata-cli generatetoaddress 6 $(debitmydata-cli -rpcwallet=payer-test getnewaddress)
sleep 12
curl -s localhost:8000/v1/payments/$PAY | jq '{status, confirmations, next_action}'
```

The last call shows `"status": "settled"` and `"next_action": "prove_wallet_control"`.

```bash
# Prove control of the key: sign the one-time challenge
CHALLENGE=$(curl -s -X POST localhost:8000/v1/dids/registrations/$REG/controller-challenges)
echo "$CHALLENGE" | jq -r .message
SIG=$(echo "$CHALLENGE" | python scripts/demo_agent.py sign agent-key.pem)
CID=$(echo "$CHALLENGE" | jq -r .challenge_id)
curl -s -X POST localhost:8000/v1/dids/registrations/$REG/controller-proofs \
  -H "Content-Type: application/json" \
  -d "{\"challenge_id\": \"$CID\", \"signature\": \"$SIG\"}" | jq
```

The proof answers `"status": "verified"` and `"next_action": "register_did"`.

```bash
# Receive the DID, the signed credential and the entitlement
curl -s -X POST localhost:8000/v1/dids/registrations \
  -H "Content-Type: application/json" \
  -d "{\"registration_intent_id\": \"$REG\"}" > /tmp/registration.json
jq '{did, entitlements, credential_url}' /tmp/registration.json

# Resolve the DID document, and check the credential with public information only
curl -s $(jq -r .did_document_url /tmp/registration.json) | jq
python scripts/demo_agent.py verify-credential /tmp/registration.json
```

The last command prints `valid: did:web:localhost%3A8000:agents:... (DemoAgent-01), signed
by did:web:localhost%3A8000`. Calling the registration endpoint again returns the same DID.
`curl -s localhost:8000/v1/dids/registrations/$REG | jq` shows the progress of a
registration at any time, including what is still missing. The `/docs` page shows every
endpoint and lets you run them from the browser.

## 5. The same registration through MCP

The gateway also serves the directive's MCP tools at `http://localhost:8000/mcp`
(Streamable HTTP), so any MCP-capable agent can do all of the above without custom code.
`scripts/demo_agent.py` can act as that MCP client. Same terminal as section 4:

```bash
python scripts/demo_agent.py mcp-tools
python scripts/demo_agent.py mcp-call dmd_get_did_registration_offer | jq '.offer.commercial_price'

QUOTE=$(python scripts/demo_agent.py mcp-call dmd_create_did_payment_quote \
  "$(jq -nc --arg key "$AGENT_KEY" '{display_name: "McpAgent-01", participant_type: "ai_agent",
        controller_public_key: $key, preferred_payment_option: "dmd_pow_native"}')")
REG=$(echo "$QUOTE" | jq -r .registration_intent_id)
PAY=$(echo "$QUOTE" | jq -r .payment_intent_id)
ADDR=$(echo "$QUOTE" | jq -r .selected_payment_option.recipient)
AMOUNT=$(echo "$QUOTE" | jq -r .selected_payment_option.amount)
```

```bash
# Pay, report the transaction (a hint only), confirm, check the status
TXID=$(debitmydata-cli -rpcwallet=payer-test sendtoaddress $ADDR $AMOUNT)
python scripts/demo_agent.py mcp-call dmd_submit_payment_reference \
  "$(jq -nc --arg pay "$PAY" --arg tx "$TXID" '{payment_intent_id: $pay, transaction_reference: $tx}')"
debitmydata-cli generatetoaddress 6 $(debitmydata-cli -rpcwallet=payer-test getnewaddress)
sleep 12
python scripts/demo_agent.py mcp-call dmd_get_payment_status \
  "$(jq -nc --arg pay "$PAY" '{payment_intent_id: $pay}')" | jq '{status, next_action}'
```

```bash
# Prove control of the key (two calls), then register
CHALLENGE=$(python scripts/demo_agent.py mcp-call dmd_prove_wallet_control \
  "$(jq -nc --arg reg "$REG" '{registration_intent_id: $reg}')")
SIG=$(echo "$CHALLENGE" | python scripts/demo_agent.py sign agent-key.pem)
CID=$(echo "$CHALLENGE" | jq -r .challenge_id)
python scripts/demo_agent.py mcp-call dmd_prove_wallet_control \
  "$(jq -nc --arg reg "$REG" --arg cid "$CID" --arg sig "$SIG" \
     '{registration_intent_id: $reg, challenge_id: $cid, signature: $sig}')" | jq '{status, next_action}'
python scripts/demo_agent.py mcp-call dmd_register_did \
  "$(jq -nc --arg reg "$REG" '{registration_intent_id: $reg}')" > /tmp/registration-mcp.json
jq '{did, entitlements}' /tmp/registration-mcp.json
python scripts/demo_agent.py verify-credential /tmp/registration-mcp.json
```

A failed call prints the same error code and details as the REST API, for example
`payment_option_unavailable` or `registration_not_ready` with the list of what is missing.

## Things worth showing

- **Nothing before payment and proof:** ask for the DID right after the quote: the answer
  is `409` and lists what is missing (`payment is awaiting_payment, not settled`,
  `controller proof is pending, not verified`).
- **Wrong key:** sign a challenge with another key
  (`python scripts/demo_agent.py new-key other-key.pem`, then sign with `other-key.pem`): the
  proof is refused, and that challenge cannot be used again, even with the right signature.
- **Underpayment:** pay less than the quoted amount and wait for the quote to expire
  (15 minutes): the status becomes `underpaid` and `refund_due` shows the amount to return.
  Nothing is credited and no DID can be issued.
- **Chain reorganization:** after settlement, undo the block that holds the payment:

  ```bash
  TX=$(curl -s localhost:8000/v1/payments/$PAY | jq -r '.transactions[0]')
  BLOCK=$(debitmydata-cli -rpcwallet=payer-test gettransaction $TX | jq -r .blockhash)
  debitmydata-cli invalidateblock $BLOCK     # the payment goes back to "confirming"
  debitmydata-cli reconsiderblock $BLOCK     # and settles again
  ```

  If the DID was already issued, the worker records the alert
  `alert.issued_identity_payment_reorged` for an operator.
- **Safety:** set `DMD_RPC_WALLET=payer-test` (a wallet that can spend) and restart the
  worker: the DMD rail refuses to run. Set `DMD_RAIL_DMD_POW_NATIVE_KILL=1` and restart
  the API: no new quotes, and no DID is issued for that rail until it is switched back on.

## Automated version

The same flow, including DID issuance, a reorg and a double-spend, runs as a test against
the node:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://gateway:gateway-dev-only@localhost:5432/agent_gateway_test
export DMD_REGTEST_RPC_URL=http://127.0.0.1:58557
export DMD_REGTEST_RPC_USER=<an RPC user that may create wallets and mine>
export DMD_REGTEST_RPC_PASSWORD=<its password>
pytest tests/test_dmd_regtest_e2e.py -v
```
