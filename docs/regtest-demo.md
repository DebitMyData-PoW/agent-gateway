# Regtest demo: an agent pays for its DID registration in DMD

This walkthrough runs the full native DMD payment flow on a private regtest chain:

1. an agent discovers DebitMyData and asks for a $25.00 DID registration quote;
2. the gateway answers with a fresh DMD address and the exact amount;
3. the agent pays;
4. the gateway detects the payment, verifies it independently, counts confirmations and
   marks it **settled** after 6 confirmations.

Regtest coins have no value and the chain only exists on your machine. Never point the
demo at a mainnet node or a real wallet.

## 1. DMD node (once)

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

The gateway refuses to start the DMD rail if its wallet can spend funds.

## 2. Gateway settings

Add to `.env` (the password is the one printed by `rpcauth.py`):

```
DMD_GW_ROUTE_REGISTRY_PATH=config/payment_routes.regtest.yaml
DMD_RAIL_DMD_POW_NATIVE_ENABLED=true
DMD_RPC_URL=http://127.0.0.1:58557
DMD_RPC_USER=gateway
DMD_RPC_PASSWORD=<rpcauth password>
DMD_RPC_WALLET=gateway-receive
DMD_STATIC_TEST_PRICE_USD=1.00
```

`DMD_STATIC_TEST_PRICE_USD` is a fixed demo price (25.00 USD / 1.00 = 25 DMD per registration).
It only works outside production and never on a mainnet rail. If `sendtoaddress` answers
"Insufficient funds", mine more blocks to the payer wallet (coins mature after 250 blocks).

## 3. Run it

Three terminals, each with the virtual environment active, from the repository root:

```bash
# Terminal 1: API
alembic upgrade head
uvicorn --factory dmd_gateway.main:create_app

# Terminal 2: payment worker
dmd-gateway-worker
```

Terminal 3, the "agent":

```bash
# Discover and see the enabled payment option
curl -s localhost:8000/v1/payment-options | jq

# Ask for a quote
QUOTE=$(curl -s -X POST localhost:8000/v1/dids/registration-quotes \
  -H "Content-Type: application/json" \
  -d '{"display_name":"DemoAgent-01","participant_type":"autonomous_agent",
       "controller_public_key":"ed25519:DEMO_PUBLIC_KEY",
       "preferred_payment_option":"dmd_pow_native"}')
echo "$QUOTE" | jq
PAY=$(echo "$QUOTE" | jq -r .payment_intent_id)
ADDR=$(echo "$QUOTE" | jq -r .selected_payment_option.recipient)
AMOUNT=$(echo "$QUOTE" | jq -r .selected_payment_option.amount)

# Pay it
debitmydata-cli -rpcwallet=payer-test sendtoaddress $ADDR $AMOUNT

# Watch the status: confirming -> verified -> settled
curl -s localhost:8000/v1/payments/$PAY | jq
debitmydata-cli generatetoaddress 1 $(debitmydata-cli -rpcwallet=payer-test getnewaddress)
curl -s localhost:8000/v1/payments/$PAY | jq '{status, confirmations}'
debitmydata-cli generatetoaddress 5 $(debitmydata-cli -rpcwallet=payer-test getnewaddress)
curl -s localhost:8000/v1/payments/$PAY | jq '{status, confirmations, next_action}'
```

The last call shows `"status": "settled"` and `"next_action": "prove_wallet_control"`.
The `/docs` page shows every endpoint and lets you run them from the browser.

## Things worth showing

- **Underpayment:** pay less than the quoted amount and wait for the quote to expire
  (15 minutes): the status becomes `underpaid` and `refund_due` shows the amount to return.
  Nothing is credited.
- **Chain reorganization:** after settlement, run
  `debitmydata-cli invalidateblock <hash of the block with the payment>`. The payment goes
  back to `confirming`. `debitmydata-cli reconsiderblock <same hash>` settles it again.
- **Safety:** set `DMD_RPC_WALLET=payer-test` (a wallet that can spend) and restart the
  worker: the DMD rail refuses to run.

## Automated version

The same flow, including the reorg and a double-spend, runs as a test against the node:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://gateway:gateway-dev-only@localhost:5432/agent_gateway_test
export DMD_REGTEST_RPC_URL=http://127.0.0.1:58557
export DMD_REGTEST_RPC_USER=<an RPC user that may create wallets and mine>
export DMD_REGTEST_RPC_PASSWORD=<its password>
pytest tests/test_dmd_regtest_e2e.py -v
```
