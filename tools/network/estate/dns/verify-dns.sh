#!/usr/bin/env bash
# Prove the serve.auto.network authoritative service (auto-g1jxw).
#
#   verify-dns.sh <host-ip> [--public]
#   verify-dns.sh <host-ip> <second-ip> [--public]   # multi-relay future
#
# Single-host rungs: TCP and UDP authoritative answers, apex + wildcard
# → relay IP, NS set, multi-value TXT atomicity through the challenge
# broker (over SSH). With a second endpoint (when a second relay machine
# carries ns2), adds serial-convergence and both-endpoint checks.
# --public (after the operator-approved delegation): ≥2 independent
# recursive resolvers resolve probe.<zone> — never trust one resolver.

set -euo pipefail
ZONE="serve.auto.network"
RELAY_IP="5.161.219.195"
PRIMARY=${1:?usage: verify-dns.sh <host-ip> [second-ip] [--public]}
shift
SECONDARY="" PUBLIC=""
for arg in "$@"; do
    case "$arg" in
    --public) PUBLIC=1 ;;
    *) SECONDARY=$arg ;;
    esac
done
FAIL=0

check() { # <desc> <cmd...>
    local desc=$1; shift
    if "$@" >/dev/null 2>&1; then echo "  ✓ $desc"
    else echo "  ✗ $desc"; FAIL=1; fi
}

q() { dig +short +time=3 +tries=1 "$@"; }
# check() invokes q inside `bash -c` subshells, which do not inherit
# shell functions unless exported — without this every rung fails with
# "q: command not found" (found the hard way in staging).
export -f q

ENDPOINTS=$PRIMARY
[ -n "$SECONDARY" ] && ENDPOINTS="$PRIMARY $SECONDARY"

echo "== authoritative answers (UDP + TCP)"
for ip in $ENDPOINTS; do
    check "@$ip UDP probe.$ZONE → $RELAY_IP" \
        bash -c "q @$ip probe.$ZONE A | grep -qx '$RELAY_IP'"
    check "@$ip TCP probe.$ZONE → $RELAY_IP" \
        bash -c "q +tcp @$ip probe.$ZONE A | grep -qx '$RELAY_IP'"
    check "@$ip apex A → $RELAY_IP" \
        bash -c "q @$ip $ZONE A | grep -qx '$RELAY_IP'"
    check "@$ip authoritative NS set" \
        bash -c "q @$ip $ZONE NS | sort | tr '\n' ' ' | grep -q 'ns1.auto.network. ns2.auto.network.'"
done

if [ -n "$SECONDARY" ]; then
    echo "== serial convergence (primary → secondary within refresh bound)"
    serial_p=$(q "@$PRIMARY" "$ZONE" SOA | awk '{print $3}')
    for _ in $(seq 60); do
        serial_s=$(q "@$SECONDARY" "$ZONE" SOA | awk '{print $3}')
        [ "$serial_s" = "$serial_p" ] && break
        sleep 5
    done
    check "secondary serial $serial_s == primary serial $serial_p" \
        test "${serial_s:-}" = "$serial_p"
fi

echo "== challenge write path: multi-value atomicity (over SSH)"
LABEL="verify-$(date +%s | tail -c 7)-aaaaaaaaaaaaaaaaaaaa"
NAME="_acme-challenge.$LABEL.$ZONE"
BROKER="cd /opt/autonomy-registry && PYTHONPATH=. venv/bin/python -m tools.network.registry.dns_challenges --db /var/lib/autonomy-registry/registry.db"
ssh -o IdentitiesOnly=yes "root@$PRIMARY" "$BROKER present $NAME apex-token && $BROKER present $NAME wildcard-token && sleep 2"
check "both TXT values live at one name" \
    bash -c "q @$PRIMARY $NAME TXT | sort | tr -d '\"' | tr '\n' ' ' | grep -q 'apex-token wildcard-token'"
ssh -o IdentitiesOnly=yes "root@$PRIMARY" "$BROKER cleanup $NAME apex-token && sleep 2"
check "one value removed, sibling preserved" \
    bash -c "q @$PRIMARY $NAME TXT | tr -d '\"' | grep -qx wildcard-token"
ssh -o IdentitiesOnly=yes "root@$PRIMARY" "$BROKER cleanup $NAME wildcard-token && sleep 2"
check "empty RRset removed" \
    bash -c "test -z \"\$(q @$PRIMARY $NAME TXT)\""

if [ -n "$PUBLIC" ]; then
    echo "== public resolution (post-cutover; majority of independent resolvers)"
    ok=0
    for resolver in 1.1.1.1 8.8.8.8 9.9.9.9 208.67.222.222; do
        if q "@$resolver" "probe.$ZONE" A | grep -qx "$RELAY_IP"; then
            echo "  ✓ @$resolver"; ok=$((ok+1))
        else
            echo "  … @$resolver (propagation?)"
        fi
    done
    check "≥2 independent resolvers agree" test "$ok" -ge 2
fi

[ "$FAIL" = 0 ] && echo "ALL CHECKS PASSED" || { echo "FAILURES PRESENT" >&2; exit 1; }
