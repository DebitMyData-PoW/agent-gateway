#!/usr/bin/env bash
# Nightly encrypted backup of the production gateway (dmd-gateway-backup.timer, as root).
#
# Backs up the database and the server's settings and keys (/etc/dmd-gateway, the signer's
# issuer key in /etc/dmd-signer, the node's settings), each encrypted with age to a public
# key whose private half is kept offline, then copies them to object storage. Nobody who
# reads the backups, the storage or this server can decrypt them without that offline key.
# On success it records the time in /var/lib/dmd-gateway-backup/last-success, which the
# monitor checks. Settings: /etc/dmd-gateway/backup.env (docs/production.md, step 4).
set -euo pipefail
umask 077

ENV_FILE=/etc/dmd-gateway/backup.env
LOCAL=/var/backups/dmd-gateway
STATUS=/var/lib/dmd-gateway-backup/last-success

[ -s "$ENV_FILE" ] || { echo "Backups are not configured: create $ENV_FILE (docs/production.md)" >&2; exit 1; }
set -a
# shellcheck disable=SC1090  # the operator's settings file
. "$ENV_FILE"
set +a
: "${DMD_BACKUP_AGE_RECIPIENT:?DMD_BACKUP_AGE_RECIPIENT is missing in $ENV_FILE}"
: "${DMD_BACKUP_REMOTE:?DMD_BACKUP_REMOTE is missing in $ENV_FILE}"
case "$DMD_BACKUP_AGE_RECIPIENT" in
    age1*) ;;
    *) echo "DMD_BACKUP_AGE_RECIPIENT must be an age public key (age1...)" >&2; exit 1 ;;
esac
KEEP_DAYS="${DMD_BACKUP_KEEP_DAYS:-30}"
DATABASE="${DMD_BACKUP_DATABASE:-agent_gateway}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$LOCAL"

db="$LOCAL/agent-gateway-db-$stamp.dump.age"
config="$LOCAL/agent-gateway-config-$stamp.tar.age"
runuser -u postgres -- pg_dump --format=custom "$DATABASE" |
    age --encrypt --recipient "$DMD_BACKUP_AGE_RECIPIENT" >"$db"
tar --create --file=- -C / etc/dmd-gateway etc/dmd-signer etc/debitmydata |
    age --encrypt --recipient "$DMD_BACKUP_AGE_RECIPIENT" >"$config"
for file in "$db" "$config"; do
    head -c 21 "$file" | grep -q '^age-encryption.org/v1' || { echo "$file is not an age file" >&2; exit 1; }
done

rclone copy -q "$LOCAL" "$DMD_BACKUP_REMOTE" --include "*-$stamp.*" --s3-no-check-bucket
rclone check -q "$LOCAL" "$DMD_BACKUP_REMOTE" --include "*-$stamp.*" --one-way --s3-no-check-bucket
rclone delete -q "$DMD_BACKUP_REMOTE" --min-age "${KEEP_DAYS}d" --include "agent-gateway-*.age" \
    --s3-no-check-bucket
find "$LOCAL" -name 'agent-gateway-*.age' -mtime +7 -delete

install -d -m 755 "$(dirname "$STATUS")"
date -u +%Y-%m-%dT%H:%M:%S+00:00 >"$STATUS"
chmod 644 "$STATUS"
echo "Backup $stamp: $(du -h "$db" | cut -f1) database, $(du -h "$config" | cut -f1) settings, copied to $DMD_BACKUP_REMOTE"
