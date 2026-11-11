#!/usr/bin/env bash
# Update the production server to the latest commit on GitHub, then restart the gateway.
# Only update production with a version that already runs well on staging.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
# The monitor and the daily report pause during the update: between "git pull" (new settings
# files) and the new code being installed, they would read new settings with old code and
# send a false alert. They start again at the end, even if the update fails.
CHECKS="dmd-gateway-monitor.timer dmd-gateway-monitor.service dmd-gateway-report.timer dmd-gateway-report.service"
# shellcheck disable=SC2086 # CHECKS is a list of unit names
$SYSTEMCTL stop $CHECKS 2>/dev/null || true
trap '$SYSTEMCTL start dmd-gateway-monitor.timer dmd-gateway-report.timer 2>/dev/null || true' EXIT
git -C "$APP_DIR" pull --ff-only
"$APP_DIR/deploy/production/setup.sh"
