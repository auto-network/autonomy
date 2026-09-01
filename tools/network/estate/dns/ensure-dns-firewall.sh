#!/usr/bin/env bash
# Ensure the DNS-role firewall exists and applies to role=dns estate VMs:
# TCP+UDP 53 from anywhere, on top of the base estate firewall (22/80/443
# stay owned by firewall-estate). Idempotent.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
. ./lib.sh
resolve_token

NAME=firewall-estate-dns
RULES='[
  {"direction":"in","protocol":"tcp","port":"53","source_ips":["0.0.0.0/0","::/0"]},
  {"direction":"in","protocol":"udp","port":"53","source_ips":["0.0.0.0/0","::/0"]}
]'

if ! hcloud firewall describe "$NAME" >/dev/null 2>&1; then
    hcloud firewall create --name "$NAME" \
        --rules-file <(echo "$RULES") \
        --apply-to label-selector \
        --apply-to-selector "managed-by=auto-network-estate,role=dns" \
        >/dev/null
    echo "created $NAME (53/tcp+udp, applies to role=dns estate VMs)"
else
    echo "$NAME already exists"
fi
