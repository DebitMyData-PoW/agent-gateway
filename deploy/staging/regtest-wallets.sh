#!/usr/bin/env bash
# Create the staging test wallets once (private regtest chain only):
#   payer-test       a customer wallet with test coins, used for demo payments
#   treasury-test    holds the keys of the receiving addresses
#   gateway-receive  the gateway's WATCH-ONLY copy of treasury-test's receive addresses
set -euo pipefail

has_wallet() { dmd-cli listwalletdir | jq -e --arg w "$1" '.wallets[] | select(.name == $w)' >/dev/null; }
loaded() { dmd-cli listwallets | jq -e --arg w "$1" 'index($w) != null' >/dev/null; }
load() { loaded "$1" || dmd-cli loadwallet "$1" true >/dev/null; }

if ! has_wallet payer-test; then
    dmd-cli -named createwallet wallet_name=payer-test load_on_startup=true >/dev/null
fi
load payer-test

if ! has_wallet treasury-test; then
    dmd-cli -named createwallet wallet_name=treasury-test load_on_startup=true >/dev/null
fi
load treasury-test

if ! has_wallet gateway-receive; then
    dmd-cli -named createwallet wallet_name=gateway-receive disable_private_keys=true \
        blank=true load_on_startup=true >/dev/null
    desc="$(dmd-cli -rpcwallet=treasury-test listdescriptors |
        jq -r '.descriptors[] | select((.desc | startswith("wpkh(")) and .internal == false) | .desc')"
    request="$(jq -nc --arg d "$desc" \
        '[{desc: $d, timestamp: "now", active: true, internal: false, range: [0, 1000]}]')"
    dmd-cli -rpcwallet=gateway-receive importdescriptors "$request" >/dev/null
fi
load gateway-receive

# Test coins for demo payments (coinbase coins mature after 250 blocks).
balance="$(dmd-cli -rpcwallet=payer-test getbalance)"
if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) < 100 else 1)" "$balance"; then
    dmd-cli generatetoaddress 260 "$(dmd-cli -rpcwallet=payer-test getnewaddress)" >/dev/null
fi
echo "payer-test balance: $(dmd-cli -rpcwallet=payer-test getbalance) DMD (test coins, no value)"
