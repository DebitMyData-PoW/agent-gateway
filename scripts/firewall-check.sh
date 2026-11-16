#!/usr/bin/env bash
# The firewall, checked from outside (roadmap SEC-10): run on your own computer, never on the
# server, and not through Cloudflare. It tries to open a connection to the server's own
# address on the ports a server like this one could expose. Only SSH must answer: HTTPS
# (443) answers Cloudflare's addresses only, and the database, the gateway API, the
# DebitMyData node's RPC and the rest answer nobody from outside. Then it asks the SSH port
# which logins it accepts, offering none itself: keys only, never a password.
#
#   scripts/firewall-check.sh <server address> <SSH port>
#   --ports "80 443 ..."   other ports to try than the usual ones
#
# The server address is its own (Vultr's page for the server), not api.debitmydata.io, which
# is Cloudflare's. IPv4 or IPv6 (run it once for each, when the server has both).
set -euo pipefail

# ftp, ssh, smtp, http, https, alternative http, MySQL, PostgreSQL, Redis, the gateway API,
# the DebitMyData node's RPC (18557), and the Bitcoin family's usual peer ports.
PORTS="21 22 25 80 443 3000 3306 5432 6379 8000 8080 8443 18557 18444 8333"
TIMEOUT_SECONDS=4

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --ports)
            [ $# -ge 2 ] || usage
            PORTS="$2"
            shift 2
            ;;
        -h | --help) usage ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done
[ "${#args[@]}" -eq 2 ] || usage
host="${args[0]#[}"
host="${host%]}"
ssh_port="${args[1]}"
case "$ssh_port" in
    '' | *[!0-9]*) usage ;;
esac
case "$host" in
    *debitmydata.io | *debitmydata.com)
        echo "$host is Cloudflare's address, not the server's: use the server's own (Vultr)." >&2
        exit 2
        ;;
esac
command -v timeout >/dev/null || { echo "timeout is missing (coreutils)" >&2; exit 2; }

results="$(mktemp -d)"
trap 'rm -rf "$results"' EXIT

# Every port at once: a dropped connection (UFW's default) takes the whole timeout.
for port in $ssh_port $PORTS; do
    (
        # shellcheck disable=SC2016  # $0 and $1 are the inner shell's: the host and port
        if timeout "$TIMEOUT_SECONDS" bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$host" "$port" \
            2>/dev/null; then
            echo open >"$results/$port"
        else
            echo closed >"$results/$port"
        fi
    ) &
done
wait

echo "Firewall of $host, from this computer ($(date -u +%Y-%m-%dT%H:%MZ)):"
unexpected=0
for port in $ssh_port $PORTS; do
    [ -f "$results/$port.shown" ] && continue
    : >"$results/$port.shown"
    state="$(cat "$results/$port")"
    if [ "$port" = "$ssh_port" ]; then
        if [ "$state" = open ]; then
            printf '  %-6s open    your SSH port, as expected\n' "$port"
        else
            printf '  %-6s closed  your SSH port: wrong address or port, or SSH is down\n' "$port"
            unexpected=1
        fi
    elif [ "$state" = open ]; then
        printf '  %-6s OPEN    not expected: anyone can reach it\n' "$port"
        unexpected=1
    else
        printf '  %-6s closed\n' "$port"
    fi
done

# The logins the SSH port accepts, as anyone would ask: offering no key and no password, the
# server answers with the list ("Permission denied (publickey)"). A password can be guessed:
# only keys must be accepted. Nothing is logged in, and nothing is tried. (FIREWALL_CHECK_SSH:
# another ssh command, for the tests.)
ssh_logins() {
    "${FIREWALL_CHECK_SSH:-ssh}" -p "$ssh_port" -o BatchMode=yes \
        -o ConnectTimeout="$TIMEOUT_SECONDS" -o PubkeyAuthentication=no \
        -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        "root@$host" true 2>&1 </dev/null |
        sed -n 's/.*Permission denied (\([^)]*\)).*/\1/p' | head -n 1
}

if [ "$(cat "$results/$ssh_port")" = open ]; then
    if ! command -v "${FIREWALL_CHECK_SSH:-ssh}" >/dev/null; then
        echo "  SSH logins: not checked (ssh is not installed here)"
    else
        logins="$(ssh_logins || true)"
        case ",$logins," in
            ,,)
                echo "  SSH logins: the server did not say which (check it on the server: sshd -T)"
                ;;
            *,password,* | *,keyboard-interactive,*)
                echo "  SSH logins: $logins  NOT EXPECTED: a password can be guessed, keys only"
                unexpected=1
                ;;
            *)
                echo "  SSH logins: $logins (keys only, as expected)"
                ;;
        esac
    fi
fi

if [ "$unexpected" -eq 0 ]; then
    echo "FIREWALL CHECK PASSED: only SSH answers from outside, with keys only."
else
    echo "FIREWALL CHECK FAILED: see the lines above (docs/emergencies.md, the firewall)." >&2
    exit 1
fi
