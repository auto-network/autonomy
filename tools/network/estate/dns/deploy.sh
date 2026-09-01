#!/usr/bin/env bash
# Deploy the serve.auto.network authoritative DNS onto the relay host
# (auto-g1jxw). The DNS server IS the registry: the responder answers
# from registry state and ships inside the registry codebase, so the
# CODE arrives via the ordinary registry deploy
# (tools/network/registry/deploy/deploy.sh). This script only installs
# and starts the separate DNS process — its own systemd unit, its own
# crash domain, same box, same store.
#
# Usage:
#   deploy.sh --host root@<relay-ip> [--node-id registry-ash-1]
#
# Idempotent; never touches the registry unit, Caddy, or coturn. The
# PARENT delegation stays out of scope: `namecheap_dns.py
# add-delegation` on the whitelisted host, operator-approved, AFTER
# verify-dns.sh passes. See README.md.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

HOST="" NODE_ID=""
while [ $# -gt 0 ]; do
    case $1 in
    --host) HOST=$2; shift 2 ;;
    --node-id) NODE_ID=$2; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done
[ -n "$HOST" ] || { echo "usage: deploy.sh --host root@<relay-ip>" >&2; exit 1; }
case "$HOST" in
*auto-ash-1*|*5.161.179.179*)
    echo "refusing: '$HOST' is the legacy pet host" >&2; exit 1 ;;
esac
HOST_IP=${HOST#*@}
NODE_ID=${NODE_ID:-registry-ash-1}

echo "== preflight: the registry code on the box must carry the responder"
ssh -o IdentitiesOnly=yes "$HOST" \
    "test -f /opt/autonomy-registry/tools/network/registry/dns_server.py" || {
    echo "dns_server.py missing under /opt/autonomy-registry —" \
         "run tools/network/registry/deploy/deploy.sh first" >&2
    exit 1
}

scp -o IdentitiesOnly=yes -q autonomy-registry-dns.service "$HOST":/tmp/
ssh -o IdentitiesOnly=yes "$HOST" bash -s <<EOF
set -euo pipefail
install -d -m 0755 /etc/autonomy-dns
cat > /etc/autonomy-dns/dns.env <<ENV
DNS_BIND=$HOST_IP
DNS_NODE_ID=$NODE_ID
ENV
install -m 0644 /tmp/autonomy-registry-dns.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q autonomy-registry-dns
systemctl restart autonomy-registry-dns
EOF

echo "== proving authoritative answers"
for _ in $(seq 15); do
    if dig +short +time=2 +tries=1 @"$HOST_IP" probe.serve.auto.network A \
            | grep -qx "$HOST_IP"; then
        echo "   @$HOST_IP answers probe.serve.auto.network → $HOST_IP ✓"
        echo "deploy complete. Next: verify-dns.sh $HOST_IP"
        exit 0
    fi
    sleep 2
done
echo "@$HOST_IP never answered; check: ssh $HOST systemctl status autonomy-registry-dns" >&2
exit 1
