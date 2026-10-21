#!/usr/bin/env bash
# Install or update the Agent Gateway STAGING server (Ubuntu 24.04, run as root).
#
# Runs on one VPS behind Cloudflare's proxy:
#   nginx (HTTPS, Cloudflare origin certificate, port 443 open to Cloudflare only)
#     -> gateway API (uvicorn, 127.0.0.1:8000) + payment worker
#     -> PostgreSQL 16 (local) and a private DebitMyData node on regtest (local RPC only)
#
# Safe to run again: existing secrets, keys, wallets and data are kept.
# See docs/staging.md for the steps before and after.
set -euo pipefail

HOSTNAME_PUBLIC="${HOSTNAME_PUBLIC:-staging-api.debitmydata.io}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILES="$APP_DIR/deploy/staging/files"
CONF_DIR=/etc/dmd-gateway
ENV_FILE="$CONF_DIR/gateway.env"
KEY_DIR="$CONF_DIR/keys"
ISSUER_KEY="$KEY_DIR/issuer-key.pem"
DMD_CONF_DIR=/etc/debitmydata
DMD_CONF="$DMD_CONF_DIR/debitmydata.conf"
DMD_DATA=/var/lib/debitmydata
CERT=/etc/ssl/cloudflare/origin.pem
CERT_KEY=/etc/ssl/cloudflare/origin.key
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
secret() { python3 -c 'import secrets; print(secrets.token_hex(32))'; }
env_value() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }

# ------------------------------------------------------------------ checks
step "Checking prerequisites"
[ "$(id -u)" -eq 0 ] || fail "run as root"
grep -q 'VERSION_ID="24.04"' /etc/os-release || fail "this script targets Ubuntu 24.04"
for bin in debitmydatad debitmydata-cli; do
    [ -x "/usr/local/bin/$bin" ] || fail "/usr/local/bin/$bin is missing: copy your DMD Core binaries there first (chmod 755)"
done
if [ ! -s "$CERT" ] || [ ! -s "$CERT_KEY" ]; then
    fail "Cloudflare origin certificate missing: save it as $CERT and its key as $CERT_KEY"
fi
chmod 644 "$CERT"
chmod 600 "$CERT_KEY"

# ------------------------------------------------------------------ packages
step "Installing packages"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1  # no prompts, no automatic service restarts
apt-get update -q
apt-get install -y -q nginx postgresql python3-venv git jq curl ufw \
    libevent-core-2.1-7t64 libevent-extra-2.1-7t64 libevent-pthreads-2.1-7t64 \
    libsqlite3-0 libzmq5 >/dev/null
missing="$(ldd /usr/local/bin/debitmydatad | grep 'not found' || true)"
[ -z "$missing" ] || fail "debitmydatad needs libraries that are not installed:\n$missing"

# ------------------------------------------------------------------ users
step "Creating service users"
id dmd >/dev/null 2>&1 || useradd --system --home-dir "$DMD_DATA" --shell /usr/sbin/nologin dmd
id gateway >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin gateway

# ------------------------------------------------------------------ secrets
step "Preparing configuration and secrets (kept if they already exist)"
install -d -m 750 -o root -g gateway "$CONF_DIR"
install -d -m 700 -o gateway -g gateway "$KEY_DIR"
RPC_PASSWORD="$(env_value DMD_RPC_PASSWORD)"
DB_PASSWORD="$(env_value DMD_GW_DATABASE_URL | sed -E 's#^[^:]+://gateway:([^@]+)@.*#\1#')"
[ -n "$RPC_PASSWORD" ] || RPC_PASSWORD="$(secret)"
[ -n "$DB_PASSWORD" ] || DB_PASSWORD="$(secret)"
if [ ! -s "$ENV_FILE" ]; then
    umask 077
    cat >"$ENV_FILE" <<EOF
# Agent Gateway staging settings. Contains secrets: readable by root only.
DMD_GW_ENVIRONMENT=staging
DMD_GW_LOG_LEVEL=INFO
DMD_GW_PUBLIC_BASE_URL=https://$HOSTNAME_PUBLIC
DMD_GW_DATABASE_URL=postgresql+psycopg://gateway:$DB_PASSWORD@127.0.0.1:5432/agent_gateway
DMD_GW_ROUTE_REGISTRY_PATH=config/payment_routes.regtest.yaml
DMD_GW_ISSUER_KEY_PATH=$ISSUER_KEY
DMD_RAIL_DMD_POW_NATIVE_ENABLED=true
DMD_RPC_URL=http://127.0.0.1:58557
DMD_RPC_USER=gateway
DMD_RPC_PASSWORD=$RPC_PASSWORD
DMD_RPC_WALLET=gateway-receive
DMD_STATIC_TEST_PRICE_USD=1.00
EOF
    umask 022
fi
chmod 600 "$ENV_FILE"

# ------------------------------------------------------------------ DMD node
step "Configuring the private DMD node (regtest)"
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
# Staging only: a private regtest chain. No peers, RPC on localhost only.
regtest=1
server=1
listen=0
[regtest]
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
fallbackfee=0.0001
rpcauth=$RPCAUTH
EOF
fi
chown root:dmd "$DMD_CONF"
chmod 640 "$DMD_CONF"
install -m 755 "$FILES/dmd-cli" /usr/local/bin/dmd-cli
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
SQL

# ------------------------------------------------------------------ application
step "Installing the gateway into $APP_DIR/.venv"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q "$APP_DIR"
if [ ! -s "$ISSUER_KEY" ]; then
    runuser -u gateway -- "$APP_DIR/.venv/bin/dmd-gateway-issuer-key" "$ISSUER_KEY" | grep -v Set
fi

run_as_gateway() {
    # shellcheck disable=SC1090  # the settings file is generated above
    (cd "$APP_DIR" && set -a && . "$ENV_FILE" && set +a && runuser -u gateway -- "$@")
}

step "Starting the DMD node"
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable --now debitmydata >/dev/null
for _ in $(seq 1 60); do dmd-cli getblockchaininfo >/dev/null 2>&1 && break; sleep 1; done
[ "$(dmd-cli getblockchaininfo | jq -r .chain)" = "regtest" ] || fail "the DMD node is not running on regtest"

step "Creating the test wallets (once)"
"$APP_DIR/deploy/staging/regtest-wallets.sh"

step "Updating the database schema"
run_as_gateway "$APP_DIR/.venv/bin/alembic" upgrade head

step "Starting the gateway API and payment worker"
sed "s#@APP_DIR@#$APP_DIR#g" "$FILES/dmd-gateway-api.service" >/etc/systemd/system/dmd-gateway-api.service
sed "s#@APP_DIR@#$APP_DIR#g" "$FILES/dmd-gateway-worker.service" >/etc/systemd/system/dmd-gateway-worker.service
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable dmd-gateway-api dmd-gateway-worker >/dev/null
$SYSTEMCTL restart dmd-gateway-api dmd-gateway-worker

# ------------------------------------------------------------------ nginx + firewall
step "Configuring nginx and the firewall (port 443 open to Cloudflare only)"
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
    fail "nginx does not answer on port 443: check 'systemctl status nginx dmd-gateway-api'"
echo
printf '\nDone. Open https://%s/v1/discovery from your computer.\n' "$HOSTNAME_PUBLIC"
