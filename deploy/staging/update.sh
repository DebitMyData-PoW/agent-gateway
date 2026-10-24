#!/usr/bin/env bash
# Update the staging server to the latest commit on GitHub, then restart the gateway.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
git -C "$APP_DIR" pull --ff-only
exec "$APP_DIR/deploy/staging/setup.sh"
