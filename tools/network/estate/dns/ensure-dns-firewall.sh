#!/usr/bin/env bash
# Ensure UDP+TCP 53 reach the relay host: the base firewall-estate only
# opens 22/80/443+icmp, so a dedicated firewall-estate-dns is created
# once and applied to the named server. Idempotent.
#
#   ensure-dns-firewall.sh [server-name]   # default registry-ash-1

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
. ./lib.sh
resolve_token

SERVER=${1:-registry-ash-1}
NAME=firewall-estate-dns
RULES='[
  {"direction":"in","protocol":"tcp","port":"53","source_ips":["0.0.0.0/0","::/0"]},
  {"direction":"in","protocol":"udp","port":"53","source_ips":["0.0.0.0/0","::/0"]}
]'

if ! hcloud firewall describe "$NAME" >/dev/null 2>&1; then
    hcloud firewall create --name "$NAME" \
        --rules-file <(echo "$RULES") >/dev/null
    echo "created $NAME (53/tcp+udp)"
fi
# Idempotent apply: attempt it, and treat "already applied" as success —
# the hcloud JSON shape for applied_to is awkward to pre-check reliably, so
# tolerate the one benign error and re-raise anything else.
if out=$(hcloud firewall apply-to-resource "$NAME" \
        --type server --server "$SERVER" 2>&1); then
    echo "applied $NAME to $SERVER"
elif printf '%s' "$out" | grep -q "already been applied"; then
    echo "$NAME already applied to $SERVER"
else
    printf '%s\n' "$out" >&2
    exit 1
fi
