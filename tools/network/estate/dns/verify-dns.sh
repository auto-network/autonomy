#!/usr/bin/env bash
# Prove the delegated serve.auto.network zone (auto-g1jxw).
#
#   verify-dns.sh <primary-ip> <secondary-ip>            # staging rungs
#   verify-dns.sh <primary-ip> <secondary-ip> --public   # + post-cutover rungs
#
# Staging rungs (no parent change needed): TCP and UDP authoritative
# answers from BOTH endpoints, wildcard → relay IP, SOA serial
# convergence primary→secondary, multi-value TXT atomicity through the
# challenge broker (over SSH to the primary).
# Public rungs (after the operator-approved delegation): ≥2 independent
# recursive resolvers resolve probe.<zone> to the relay IP — never trust
# one resolver (the estate lesson).

set -euo pipefail
ZONE="serve.auto.network"
RELAY_IP="5.161.219.195"
PRIMARY=${1:?usage: verify-dns.sh <primary-ip> <secondary-ip> [--public]}
SECONDARY=${2:?usage: verify-dns.sh <primary-ip> <secondary-ip> [--public]}
PUBLIC=${3:-}
FAIL=0

check() { # <desc> <cmd...>
    local desc=$1; shift
    if "$@" >/dev/null 2>&1; then echo "  ✓ $desc"
    else echo "  ✗ $desc"; FAIL=1; fi
}

q() { dig +short +time=3 +tries=1 "$@"; }

echo "== authoritative answers (UDP + TCP, both endpoints)"
for ip in "$PRIMARY" "$SECONDARY"; do
    check "@$ip UDP probe.$ZONE → $RELAY_IP" \
        bash -c "q @$ip probe.$ZONE A | grep -qx '$RELAY_IP'"
    check "@$ip TCP probe.$ZONE → $RELAY_IP" \
        bash -c "q +tcp @$ip probe.$ZONE A | grep -qx '$RELAY_IP'"
    check "@$ip apex A → $RELAY_IP" \
        bash -c "q @$ip $ZONE A | grep -qx '$RELAY_IP'"
    check "@$ip authoritative NS set" \
        bash -c "q @$ip $ZONE NS | sort | tr '\n' ' ' | grep -q 'ns1.$ZONE. ns2.$ZONE.'"
done

echo "== serial convergence (primary → secondary within refresh bound)"
serial_p=$(q "@$PRIMARY" "$ZONE" SOA | awk '{print $3}')
for _ in $(seq 60); do
    serial_s=$(q "@$SECONDARY" "$ZONE" SOA | awk '{print $3}')
    [ "$serial_s" = "$serial_p" ] && break
    sleep 5
done
check "secondary serial $serial_s == primary serial $serial_p" \
    test "${serial_s:-}" = "$serial_p"

echo "== challenge broker: multi-value atomicity (over SSH to primary)"
LABEL="verify-$(date +%s | tail -c 7)-aaaaaaaaaaaaaaaaaaaa"
NAME="_acme-challenge.$LABEL.$ZONE"
BROKER="python3 /usr/local/bin/challenge_broker.py"
ssh -o IdentitiesOnly=yes "root@$PRIMARY" "$BROKER present $NAME apex-token && $BROKER present $NAME wildcard-token"
check "both TXT values live at one name" \
    bash -c "q @$PRIMARY $NAME TXT | sort | tr -d '\"' | tr '\n' ' ' | grep -q 'apex-token wildcard-token'"
ssh -o IdentitiesOnly=yes "root@$PRIMARY" "$BROKER cleanup $NAME apex-token"
check "one value removed, sibling preserved" \
    bash -c "q @$PRIMARY $NAME TXT | tr -d '\"' | grep -qx wildcard-token"
ssh -o IdentitiesOnly=yes "root@$PRIMARY" "$BROKER cleanup $NAME wildcard-token"
check "empty RRset removed" \
    bash -c "test -z \"\$(q @$PRIMARY $NAME TXT)\""

if [ "$PUBLIC" = "--public" ]; then
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
