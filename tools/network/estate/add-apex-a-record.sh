#!/usr/bin/env bash
# Add the auto.network APEX A record → 5.161.219.195 (registry-ash-1).
#
# This is the single authorized DNS mutation for the public front door
# (bead auto-9q7a5; OPERATOR RULING 2026-08-13). It is a thin, pinned wrapper
# over namecheap_dns.py so the dangerous logic (read-modify-write, critical-
# record gate, %2B DKIM encoding, exactly-one-addition verify) lives in one
# tested place and this file only names WHAT to add.
#
# WHERE: run ON auto-ash-1 (5.161.179.179). Namecheap whitelists that box's
#        own IP as the API client IP; from anywhere else the call is refused.
#        ssh -o IdentitiesOnly=yes -i ~/.ssh/auto root@5.161.179.179
#
# ORDERING (why DNS is first): registry-ash-1's Caddy mints the auto.network
#        certificate over HTTP-01, which requires auto.network to ALREADY
#        resolve to 5.161.219.195. So this DNS record must land and propagate
#        BEFORE the apex Caddy vhost is deployed (deploy-apex-vhost.sh) — never
#        the other way round, or cert issuance fails against a name that does
#        not yet point at the box.
#
# Usage:
#   ./add-apex-a-record.sh            # perform the change (read→gate→write→verify)
#   ./add-apex-a-record.sh --dry-run  # read + gate + encoding-check only, no write
#
# Rollback: the pre-change record set is saved verbatim to
#   /var/backups/namecheap/auto.network.before.xml
# Re-applying that exact set via setHosts restores DNS. Keep it until the
# front door is confirmed healthy.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

APEX_IP=5.161.219.195   # registry-ash-1 — the box whose Caddy fronts /install
SAVE_DIR=/var/backups/namecheap

# Prefer a python with a recent stdlib (xml/urllib are always present, but keep
# parity with lib.sh's interpreter selection so behaviour matches the estate).
PY=python3
for c in python3.13 python3.12 python3.11 python3; do
    command -v "$c" >/dev/null 2>&1 && { PY=$c; break; }
done

exec "$PY" namecheap_dns.py add-record \
    --sld auto --tld network \
    --name @ --type A --address "$APEX_IP" --ttl 1800 \
    --save-dir "$SAVE_DIR" \
    "$@"
