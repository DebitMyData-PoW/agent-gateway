#!/usr/bin/env bash
# The backups' restore test (docs/production.md, step 4e): on your own computer, never on the
# server. It reads one nightly pair (agent-gateway-db-<time>.dump.age and
# agent-gateway-config-<time>.tar.age) with the offline backup key and checks that both can be
# restored, without a database and without writing anything decrypted to disk:
#
#   - the settings: every file in the archive, by name, with the ones a new server needs;
#   - the database: the dump's details and the rows in each table, for comparing with the
#     server.
#
#   scripts/backup-restore-test.sh <folder with the copied pairs>   the newest pair in it
#   scripts/backup-restore-test.sh <db file> <config file>          that pair
#   --key-file FILE   the backup key's file; otherwise the key is asked for (pasted from the
#                     password manager, not shown, and gone when the test ends)
#
# Needs age and pg_restore (sudo apt install age postgresql-client).
set -euo pipefail
umask 077

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

fail() {
    echo "FAILED: $*" >&2
    exit 1
}

# Decrypts $1 with the key into the command that follows, which may stop reading before the
# end (pg_restore --list, tar -t): the rest is read and dropped, so that age is not cut off
# mid-file (SIGPIPE) and taken for a failed decryption. Errors: $private/age.err and
# $private/tool.err.
decrypt_into() {
    local file="$1"
    shift
    age --decrypt --identity "$key_file" "$file" 2>"$private/age.err" | (
        status=0
        "$@" 2>"$private/tool.err" || status=$?
        cat >/dev/null
        exit "$status"
    )
}

errors() {
    cat "$private/age.err" "$private/tool.err" 2>/dev/null | tr '\n' ' '
}

key_file=""
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --key-file)
            [ $# -ge 2 ] || usage
            key_file="$2"
            shift 2
            ;;
        -h | --help) usage ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

for tool in age age-keygen pg_restore tar; do
    command -v "$tool" >/dev/null || fail "$tool is missing: sudo apt install age postgresql-client"
done

# ------------------------------------------------------------------ the pair
if [ "${#args[@]}" -eq 1 ] && [ -d "${args[0]}" ]; then
    db="$(find "${args[0]}" -maxdepth 1 -name 'agent-gateway-db-*.dump.age' | sort | tail -n 1)"
    [ -n "$db" ] || fail "no agent-gateway-db-<time>.dump.age in ${args[0]}"
    stamp="${db##*/agent-gateway-db-}"
    stamp="${stamp%.dump.age}"
    config="${args[0]%/}/agent-gateway-config-$stamp.tar.age"
elif [ "${#args[@]}" -eq 2 ]; then
    db="${args[0]}"
    config="${args[1]}"
else
    usage
fi
for file in "$db" "$config"; do
    [ -f "$file" ] || fail "$file is not there"
    head -c 21 "$file" | grep -q '^age-encryption.org/v1' || fail "$file is not an age file"
done
db_stamp="$(basename "$db")"
db_stamp="${db_stamp#agent-gateway-db-}"
db_stamp="${db_stamp%.dump.age}"
config_stamp="$(basename "$config")"
config_stamp="${config_stamp#agent-gateway-config-}"
config_stamp="${config_stamp%.tar.age}"
[ "$db_stamp" = "$config_stamp" ] ||
    fail "the two files are from different nights ($db_stamp and $config_stamp)"

# ------------------------------------------------------------------ the key
# In memory where the system offers it (/dev/shm), never next to the backups.
scratch="${TMPDIR:-/tmp}"
if [ -d /dev/shm ] && [ -w /dev/shm ]; then
    scratch=/dev/shm
fi
private="$(mktemp -d "$scratch/restore-test.XXXXXX")"
trap 'rm -rf "$private"' EXIT
if [ -z "$key_file" ]; then
    echo "Paste the backup key's AGE-SECRET-KEY-1... line (from the password manager)."
    printf 'It is not shown, and it is deleted when the test ends: '
    IFS= read -rs secret
    echo
    case "$secret" in
        AGE-SECRET-KEY-1*) ;;
        *) fail "that is not an age secret key (it starts with AGE-SECRET-KEY-1)" ;;
    esac
    key_file="$private/key.txt"
    printf '%s\n' "$secret" >"$key_file"
    unset secret
fi
[ -r "$key_file" ] || fail "cannot read the key file $key_file"
public_key="$(age-keygen -y "$key_file" 2>/dev/null)" || fail "$key_file is not an age key"

echo
echo "Backup of $db_stamp"
echo "Backup key: $public_key"
echo "  (the server encrypts to DMD_BACKUP_AGE_RECIPIENT in /etc/dmd-gateway/backup.env)"

# ------------------------------------------------------------------ the settings
echo
echo "SETTINGS AND KEYS ($(basename "$config"))"
names="$private/names.txt"
decrypt_into "$config" tar -tf - >"$names" ||
    fail "the settings archive cannot be read: $(errors)"
sed 's/^/  /' "$names"
missing=0
for needed in etc/dmd-gateway/gateway.env etc/dmd-signer/signer.env etc/dmd-signer/issuer-key.pem; do
    if ! grep -qx "$needed" "$names"; then
        echo "  MISSING: /$needed" >&2
        missing=1
    fi
done
[ "$missing" -eq 0 ] || fail "the settings backup lacks what a new server needs (above)"
echo "  ok: the settings, the signer's settings and the issuer key are there"

# ------------------------------------------------------------------ the database
echo
echo "DATABASE ($(basename "$db"))"
decrypt_into "$db" pg_restore --list >"$private/toc.txt" ||
    fail "the database dump cannot be read: $(errors)(pg_restore must be at least the server's PostgreSQL version, 16; this one: $(pg_restore --version))"
grep -E '^; +(Archive created at |dbname:|Dumped from database version:)' "$private/toc.txt" |
    sed 's/^; */  /'
decrypt_into "$db" pg_restore --data-only --file=- |
    awk '
        /^COPY / { table = $2; sub(/^public\./, "", table); rows = 0; inside = 1; next }
        inside && /^\\\.$/ { printf "  %-26s %d\n", table, rows; inside = 0; next }
        inside { rows++ }
    ' >"$private/rows.txt" || fail "the database's rows cannot be read: $(errors)"
[ -s "$private/rows.txt" ] || fail "the dump holds no table"
grep -q '^  identities ' "$private/rows.txt" || fail "the dump has no identities table"
echo "  rows per table:"
sed 's/^/  /' "$private/rows.txt"

identities="$(awk '$1 == "identities" { print $2 }' "$private/rows.txt")"
echo
echo "RESTORE TEST PASSED: both files of $db_stamp can be restored with this key."
echo "Compare with the server (it has the same or more: it kept working after the backup):"
echo "  sudo -u postgres psql -d agent_gateway -Atc 'SELECT count(*) FROM identities'   # backup: $identities"
