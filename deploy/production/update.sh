#!/usr/bin/env bash
# Update the production server to the latest commit on GitHub, then restart the gateway.
# Only update production with a version that already runs well on staging.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
git -C "$APP_DIR" pull --ff-only
exec "$APP_DIR/deploy/production/setup.sh"
