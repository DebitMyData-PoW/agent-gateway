#!/usr/bin/env bash
# Install or update the Agent Gateway PRODUCTION server (Ubuntu 24.04, run as root).
#
# One VPS behind Cloudflare's proxy, with real payments:
#   nginx (HTTPS, Cloudflare origin certificate, port 443 open to Cloudflare only)
#     -> gateway API (uvicorn, 127.0.0.1:8000) + payment worker + monitor
#     -> PostgreSQL 16 (local) and a DebitMyData node on the main chain (local RPC only,
#        no inbound connections), holding only the treasury's watch-only key
#   credential signer: its own user, no network, the only process that can read the
#     issuer key; the API reaches it through a local socket
#   nightly encrypted backup to object storage
#
# Nothing here is for testing: no test tools, no test chain, no test wallets. Every payment
# option starts switched OFF; docs/production.md says when and how to switch each one on.
# Safe to run again: existing secrets, keys, wallets and data are kept.
set -euo pipefail

HOSTNAME_PUBLIC="${HOSTNAME_PUBLIC:-api.debitmydata.io}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILES="$APP_DIR/deploy/production/files"
SHARED="$APP_DIR/deploy/staging/files"  # units and snippets that are the same on both servers
CONF_DIR=/etc/dmd-gateway
ENV_FILE="$CONF_DIR/gateway.env"
SIGNER_DIR=/etc/dmd-signer
SIGNER_ENV="$SIGNER_DIR/signer.env"
ISSUER_KEY="$SIGNER_DIR/issuer-key.pem"
SIGNER_SOCKET=/run/dmd-signer/signer.sock
DMD_CONF_DIR=/etc/debitmydata
DMD_CONF="$DMD_CONF_DIR/debitmydata.conf"
DMD_DATA=/var/lib/debitmydata
BACKUP_STATUS=/var/lib/dmd-gateway-backup/last-success
CERT=/etc/ssl/cloudflare/origin.pem
CERT_KEY=/etc/ssl/cloudflare/origin.key
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
VENV="$APP_DIR/.venv"

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
secret() { python3 -c 'import secrets; print(secrets.token_hex(32))'; }
env_value() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }

# ------------------------------------------------------------------ checks
step "Checking prerequisites"
[ "$(id -u)" -eq 0 ] || fail "run as root"
grep -q 'VERSION_ID="24.04"' /etc/os-release || fail "this script targets Ubuntu 24.04"
for bin in debitmydatad debitmydata-cli; do
    [ -x "/usr/local/bin/$bin" ] || fail "/usr/local/bin/$bin is missing: copy the DebitMyData Core v1.3.3 binaries there first (chmod 755)"
done
if [ ! -s "$CERT" ] || [ ! -s "$CERT_KEY" ]; then
    fail "Cloudflare origin certificate missing: save it as $CERT and its key as $CERT_KEY"
fi
chmod 644 "$CERT"
chmod 600 "$CERT_KEY"
if [ -s "$ENV_FILE" ] && [ "$(env_value DMD_GW_ENVIRONMENT)" != "production" ]; then
    fail "$ENV_FILE is not a production settings file: this script is for the production server only"
fi

# ------------------------------------------------------------------ packages
step "Installing packages"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1  # no prompts, no automatic service restarts
apt-get update -q
apt-get install -y -q nginx postgresql python3-venv git jq curl ufw age rclone \
    libevent-core-2.1-7t64 libevent-extra-2.1-7t64 libevent-pthreads-2.1-7t64 \
    libsqlite3-0 libzmq5 >/dev/null
missing="$(ldd /usr/local/bin/debitmydatad | grep 'not found' || true)"
[ -z "$missing" ] || fail "debitmydatad needs libraries that are not installed:\n$missing"

# ------------------------------------------------------------------ users
step "Creating service users"
id dmd >/dev/null 2>&1 || useradd --system --home-dir "$DMD_DATA" --shell /usr/sbin/nologin dmd
id gateway >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin gateway
id dmd_signer >/dev/null 2>&1 ||
    useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin dmd_signer
# The gateway may open the signer's socket (and nothing else of the signer's: its key is
# readable by the signer only).
usermod -aG dmd_signer gateway

# ------------------------------------------------------------------ secrets
step "Preparing configuration and secrets (kept if they already exist)"
install -d -m 750 -o root -g gateway "$CONF_DIR"
install -d -m 700 -o dmd_signer -g dmd_signer "$SIGNER_DIR"
RPC_PASSWORD="$(env_value DMD_RPC_PASSWORD)"
DB_PASSWORD="$(env_value DMD_GW_DATABASE_URL | sed -E 's#^[^:]+://gateway:([^@]+)@.*#\1#')"
[ -n "$RPC_PASSWORD" ] || RPC_PASSWORD="$(secret)"
[ -n "$DB_PASSWORD" ] || DB_PASSWORD="$(secret)"
if [ ! -s "$ENV_FILE" ]; then
    umask 077
    cat >"$ENV_FILE" <<EOF
# Agent Gateway PRODUCTION settings. Contains secrets: readable by root only.
# Each value alone on its line; values with spaces, < > or \$ in single quotes ('...').
DMD_GW_ENVIRONMENT=production
DMD_GW_LOG_LEVEL=INFO
DMD_GW_PUBLIC_BASE_URL=https://$HOSTNAME_PUBLIC
DMD_GW_DATABASE_URL=postgresql+psycopg://gateway:$DB_PASSWORD@127.0.0.1:5432/agent_gateway
DMD_GW_ROUTE_REGISTRY_PATH=config/payment_routes.yaml
DMD_GW_ISSUER_SIGNER_SOCKET=$SIGNER_SOCKET
DMD_GW_DMD_PRICE_CHAIN_DATA_FROM=node
DMD_GW_BACKUP_STATUS_PATH=$BACKUP_STATUS

# DebitMyData coin (DMD): switched on once the treasury's watch-only key is loaded
# (DMD Treasury Wallet guide, section 5; docs/production.md, step 6).
DMD_RAIL_DMD_POW_NATIVE_ENABLED=false
DMD_RPC_URL=http://127.0.0.1:18557
DMD_RPC_USER=gateway
DMD_RPC_PASSWORD=$RPC_PASSWORD
DMD_RPC_WALLET=gateway-receive

# USDC through x402 on Base: switched on once the Coinbase (CDP) API key is set
# (docs/production.md, step 7). Receiving wallets: AgenticSpaces DID-LLM (DID payments)
# and AgenticInference Pay (inference donations).
DMD_RAIL_X402_USDC_BASE_ENABLED=false
DMD_USDC_BASE_MERCHANT_ADDRESS=0x67C11dc63B45b916213189BF127094d4662a2eC3
DMD_USDC_BASE_INFERENCE_ADDRESS=0x3BA9A001ec76Dc24384f8dEE8bE3479cE45bD42E
DMD_X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
DMD_BASE_RPC_URL=https://mainnet.base.org
DMD_X402_CDP_API_KEY_ID=
DMD_X402_CDP_API_KEY_SECRET=
EOF
    umask 022
fi
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ------------------------------------------------------------------ DebitMyData node
step "Configuring the DebitMyData node (main chain)"
install -d -m 750 -o dmd -g dmd "$DMD_DATA"
install -d -m 750 -o root -g dmd "$DMD_CONF_DIR"
if [ ! -s "$DMD_CONF" ]; then
    # shellcheck disable=SC2016  # Python code, not shell
    RPCAUTH="$(RPC_PASSWORD="$RPC_PASSWORD" python3 -c '
import hmac, os, secrets
salt = secrets.token_hex(16)
digest = hmac.new(salt.encode(), os.environ["RPC_PASSWORD"].encode(), "sha256").hexdigest()
print(f"gateway:{salt}${digest}")')"
    cat >"$DMD_CONF" <<EOF
# Production: the DebitMyData main chain. RPC on this server only; outbound peers only.
server=1
# The network's seed nodes, as in DebitMyData Core's own default settings.
seednode=dnsseed.debitmydata.network
seednode=dnsseed2.debitmydata.network
$(for n in $(seq 1 10); do printf 'addnode=seed%s.debitmydata.network\n' "$n"; done)
listen=0
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=18557
walletbroadcast=0
rpcauth=$RPCAUTH
EOF
fi
chown root:dmd "$DMD_CONF"
chmod 640 "$DMD_CONF"
grep -q '^regtest=1' "$DMD_CONF" && fail "$DMD_CONF is set for regtest: production runs on the main chain"
install -m 755 "$SHARED/dmd-cli" /usr/local/bin/dmd-cli
install -m 644 "$FILES/debitmydata.service" /etc/systemd/system/debitmydata.service

# ------------------------------------------------------------------ database
step "Configuring PostgreSQL"
$SYSTEMCTL enable --now postgresql >/dev/null
for _ in $(seq 1 30); do runuser -u postgres -- pg_isready -q && break; sleep 1; done
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -v pw="$DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE gateway LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gateway') \gexec
SELECT format('ALTER ROLE gateway LOGIN PASSWORD %L', :'pw') \gexec
SELECT 'CREATE DATABASE agent_gateway OWNER gateway'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'agent_gateway') \gexec
-- The signer's read-only role: no password, it connects through the local socket as the
-- dmd_signer system user (peer authentication).
SELECT 'CREATE ROLE dmd_signer LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dmd_signer') \gexec
SQL

# ------------------------------------------------------------------ application
step "Installing the gateway into $VENV"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "$APP_DIR"

bash -n "$ENV_FILE" 2>/dev/null ||
    fail "$ENV_FILE has a syntax error: put values with spaces, < > or \$ in single quotes ('...')"

step "Preparing the credential signer and its key (created once, never shown)"
ISSUER_DID="$("$VENV/bin/python" -c 'import sys; from dmd_gateway.identity.did import issuer_did; print(issuer_did(sys.argv[1]))' "https://$HOSTNAME_PUBLIC")"
if [ ! -s "$ISSUER_KEY" ]; then
    runuser -u dmd_signer -- "$VENV/bin/dmd-gateway-issuer-key" "$ISSUER_KEY" >/dev/null
fi
chown dmd_signer:dmd_signer "$ISSUER_KEY"
chmod 600 "$ISSUER_KEY"
# shellcheck disable=SC2016  # Python code, not shell
PUBLIC_KEY="$(runuser -u dmd_signer -- "$VENV/bin/python" -c '
import sys
from pathlib import Path
from dmd_gateway.identity.issuer import load_issuer_key
print(load_issuer_key(Path(sys.argv[1]), sys.argv[2]).public_key_multibase)' "$ISSUER_KEY" "$ISSUER_DID")"
CONFIGURED="$(env_value DMD_GW_ISSUER_PUBLIC_KEY)"
if [ -z "$CONFIGURED" ]; then
    printf 'DMD_GW_ISSUER_PUBLIC_KEY=%s\n' "$PUBLIC_KEY" >>"$ENV_FILE"
elif [ "$CONFIGURED" != "$PUBLIC_KEY" ]; then
    fail "the issuer key in $ISSUER_KEY does not match DMD_GW_ISSUER_PUBLIC_KEY: it was replaced. Restore the original key (docs/production.md, backups); a new key would make every credential issued so far unverifiable"
fi
cat >"$SIGNER_ENV" <<EOF
DMD_SIGNER_KEY_PATH=$ISSUER_KEY
DMD_SIGNER_SOCKET=$SIGNER_SOCKET
DMD_SIGNER_DATABASE_URL=postgresql+psycopg://dmd_signer@/agent_gateway?host=/var/run/postgresql
DMD_SIGNER_ISSUER_DID=$ISSUER_DID
EOF
chown root:dmd_signer "$SIGNER_ENV"
chmod 640 "$SIGNER_ENV"

run_as_gateway() {
    # shellcheck disable=SC1090  # the settings file is generated above
    (cd "$APP_DIR" && set -a && . "$ENV_FILE" && set +a && runuser -u gateway -- "$@")
}

step "Starting the DebitMyData node"
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable --now debitmydata >/dev/null
for _ in $(seq 1 60); do dmd-cli getblockchaininfo >/dev/null 2>&1 && break; sleep 1; done
[ "$(dmd-cli getblockchaininfo | jq -r .chain)" = "main" ] || fail "the DebitMyData node is not on the main chain"

step "Updating the database schema"
run_as_gateway "$VENV/bin/alembic" upgrade head
# What the signer may read to check a credential before signing it (and nothing else).
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -d agent_gateway <<'SQL'
GRANT CONNECT ON DATABASE agent_gateway TO dmd_signer;
GRANT USAGE ON SCHEMA public TO dmd_signer;
GRANT SELECT ON registration_intents, payment_intents, controller_challenges,
    controller_proofs, credentials, purchases TO dmd_signer;
SQL

step "Starting the credential signer, the gateway API and the payment worker"
sed "s#@APP_DIR@#$APP_DIR#g" "$FILES/dmd-gateway-signer.service" >/etc/systemd/system/dmd-gateway-signer.service
# One API process per CPU core, between 2 and 4 (each one keeps up to 15 database connections).
API_WORKERS="$(nproc)"
if [ "$API_WORKERS" -lt 2 ]; then API_WORKERS=2; fi
if [ "$API_WORKERS" -gt 4 ]; then API_WORKERS=4; fi
sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@API_WORKERS@#$API_WORKERS#g" \
    "$SHARED/dmd-gateway-api.service" >/etc/systemd/system/dmd-gateway-api.service
sed "s#@APP_DIR@#$APP_DIR#g" "$SHARED/dmd-gateway-worker.service" >/etc/systemd/system/dmd-gateway-worker.service
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable dmd-gateway-signer dmd-gateway-api dmd-gateway-worker >/dev/null
$SYSTEMCTL restart dmd-gateway-signer
$SYSTEMCTL restart dmd-gateway-api dmd-gateway-worker

step "Starting the monitor (every 2 minutes), the daily report (07:00 UTC) and the backup (03:30 UTC)"
for unit in dmd-gateway-monitor.service dmd-gateway-monitor.timer \
    dmd-gateway-report.service dmd-gateway-report.timer; do
    sed "s#@APP_DIR@#$APP_DIR#g" "$SHARED/$unit" >"/etc/systemd/system/$unit"
done
for unit in dmd-gateway-backup.service dmd-gateway-backup.timer; do
    sed "s#@APP_DIR@#$APP_DIR#g" "$FILES/$unit" >"/etc/systemd/system/$unit"
done
install -d -m 755 "$(dirname "$BACKUP_STATUS")"
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable --now dmd-gateway-monitor.timer dmd-gateway-report.timer \
    dmd-gateway-backup.timer >/dev/null

# ------------------------------------------------------------------ nginx + firewall
step "Configuring nginx and the firewall (port 443 open to Cloudflare only)"
install -m 644 "$SHARED/nginx-proxy.conf" /etc/nginx/snippets/dmd-gateway-proxy.conf
sed "s#@HOSTNAME@#$HOSTNAME_PUBLIC#g" "$FILES/nginx-site.conf" >/etc/nginx/sites-available/dmd-gateway
if [ ! -e /proc/net/if_inet6 ]; then  # no IPv6 on this server: listen on IPv4 only
    sed -i '/listen \[::\]/d' /etc/nginx/sites-available/dmd-gateway
fi
ln -sf /etc/nginx/sites-available/dmd-gateway /etc/nginx/sites-enabled/dmd-gateway
rm -f /etc/nginx/sites-enabled/default
"$APP_DIR/deploy/staging/cloudflare-ips.sh"

step "Checking the gateway through nginx"
for _ in $(seq 1 30); do
    curl -sk --noproxy '*' --resolve "$HOSTNAME_PUBLIC:443:127.0.0.1" "https://$HOSTNAME_PUBLIC/readyz" |
        grep '"ready"' >/dev/null && break
    sleep 1
done
curl -sk --noproxy '*' --resolve "$HOSTNAME_PUBLIC:443:127.0.0.1" "https://$HOSTNAME_PUBLIC/readyz" ||
    fail "nginx does not answer on port 443: check 'systemctl status nginx dmd-gateway-api dmd-gateway-signer'"
echo

SYNC="$(dmd-cli getblockchaininfo | jq -r '"block \(.blocks) of \(.headers), \(.verificationprogress * 100 | floor)% verified"')"
printf '\nDone. Production gateway: https://%s\n' "$HOSTNAME_PUBLIC"
printf 'DebitMyData node: %s (wait for 100%% before switching DMD payments on).\n' "$SYNC"
printf 'Issuer key (public, published at /.well-known/did.json): %s\n' "$PUBLIC_KEY"
printf 'All payment options are OFF until switched on: see docs/production.md, steps 5 to 9.\n'
