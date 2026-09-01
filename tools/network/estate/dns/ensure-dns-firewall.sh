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
if ! hcloud firewall describe "$NAME" -o json \
        | grep -q "\"name\": \"$SERVER\""; then
    hcloud firewall apply-to-resource "$NAME" \
        --type server --server "$SERVER"
    echo "applied $NAME to $SERVER"
else
    echo "$NAME already applied to $SERVER"
fi
