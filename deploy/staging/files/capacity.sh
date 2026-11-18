# shellcheck shell=bash
# Sourced by setup.sh, on staging and production: how many API processes and payment workers
# this server runs, and enough PostgreSQL connections for all of them.
#   needs: ENV_FILE, SYSTEMCTL, fail
#   sets:  API_WORKERS, PAYMENT_WORKERS
#
# One API process per CPU core, between 2 and 16. One payment worker per core, between 2 and 8,
# unless DMD_GW_PAYMENT_WORKERS (1 to 32) says otherwise: checking payments mostly waits on
# the node and the payment companies, so more workers than cores can help.

# A setting missing from the settings file (the usual case) is empty, not an error: setup.sh runs
# with pipefail, where grep finding nothing would stop it before the gateway restarts.
capacity_setting() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -dc '0-9' || true; }

CORES="$(nproc)"
API_WORKERS="$CORES"
if [ "$API_WORKERS" -lt 2 ]; then API_WORKERS=2; fi
if [ "$API_WORKERS" -gt 16 ]; then API_WORKERS=16; fi

PAYMENT_WORKERS="$(capacity_setting DMD_GW_PAYMENT_WORKERS)"
if [ -z "$PAYMENT_WORKERS" ]; then
    PAYMENT_WORKERS="$CORES"
    if [ "$PAYMENT_WORKERS" -lt 2 ]; then PAYMENT_WORKERS=2; fi
    if [ "$PAYMENT_WORKERS" -gt 8 ]; then PAYMENT_WORKERS=8; fi
fi
if [ "$PAYMENT_WORKERS" -lt 1 ] || [ "$PAYMENT_WORKERS" -gt 32 ]; then
    fail "DMD_GW_PAYMENT_WORKERS must be between 1 and 32"
fi

# Each process keeps up to DMD_GW_DATABASE_POOL_SIZE + DMD_GW_DATABASE_MAX_OVERFLOW
# connections (5 + 5); a payment worker uses at most 3 of them. 40 more for the monitor, the
# report, the signer, backups and an operator's psql.
POOL="$(capacity_setting DMD_GW_DATABASE_POOL_SIZE)"
OVERFLOW="$(capacity_setting DMD_GW_DATABASE_MAX_OVERFLOW)"
PER_PROCESS=$((${POOL:-5} + ${OVERFLOW:-5}))
PER_WORKER=$((PER_PROCESS < 3 ? PER_PROCESS : 3))
NEEDED=$((API_WORKERS * PER_PROCESS + PAYMENT_WORKERS * PER_WORKER + 40))
if [ "$NEEDED" -lt 100 ]; then NEEDED=100; fi
CURRENT="$(runuser -u postgres -- psql -tAc 'SHOW max_connections')"
if [ "$CURRENT" -lt "$NEEDED" ]; then
    # Takes effect when PostgreSQL restarts: a few seconds, before the gateway's own restart.
    runuser -u postgres -- psql -q -c "ALTER SYSTEM SET max_connections = $NEEDED"
    $SYSTEMCTL restart postgresql
    for _ in $(seq 1 30); do runuser -u postgres -- pg_isready -q && break; sleep 1; done
    CURRENT="$(runuser -u postgres -- psql -tAc 'SHOW max_connections')"
fi
echo "    $API_WORKERS API processes, $PAYMENT_WORKERS payment workers, $CURRENT database connections at most"
