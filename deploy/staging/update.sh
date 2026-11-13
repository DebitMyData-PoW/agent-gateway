#!/usr/bin/env bash
# Update the staging server to the latest commit on GitHub, then restart the gateway.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
ENV_FILE="${DMD_GW_ENV_FILE:-/etc/dmd-gateway/gateway.env}"
# Never on the production server: staging's setup would add its test settings to production's.
if grep -qE "^DMD_GW_ENVIRONMENT=[\"']?production[\"']?[[:space:]]*$" "$ENV_FILE" 2>/dev/null; then
    echo "ERROR: $ENV_FILE says DMD_GW_ENVIRONMENT=production: this is the production server." >&2
    echo "Nothing was changed. To update production: $APP_DIR/deploy/production/update.sh" >&2
    exit 1
fi
# The monitor and the daily report pause during the update: between "git pull" (new settings
# files) and the new code being installed, they would read new settings with old code and
# send a false alert. They start again at the end, even if the update fails.
CHECKS="dmd-gateway-monitor.timer dmd-gateway-monitor.service dmd-gateway-report.timer dmd-gateway-report.service"
# shellcheck disable=SC2086 # CHECKS is a list of unit names
$SYSTEMCTL stop $CHECKS 2>/dev/null || true
trap '$SYSTEMCTL start dmd-gateway-monitor.timer dmd-gateway-report.timer 2>/dev/null || true' EXIT
git -C "$APP_DIR" pull --ff-only
"$APP_DIR/deploy/staging/setup.sh"
