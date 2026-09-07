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

HOST="" NODE_ID="" RELAY_IP=""
while [ $# -gt 0 ]; do
    case $1 in
    --host) HOST=$2; shift 2 ;;
    --node-id) NODE_ID=$2; shift 2 ;;
    --relay-ip) RELAY_IP=$2; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done
[ -n "$HOST" ] || { echo "usage: deploy.sh --host root@<relay-ip> [--relay-ip IP]" >&2; exit 1; }
case "$HOST" in
*auto-ash-1*|*5.161.179.179*)
    echo "refusing: '$HOST' is the legacy pet host" >&2; exit 1 ;;
esac
HOST_NAME=${HOST#*@}
# --host may name the box (root@registry.auto.network). Everything below
# needs its IPv4: DNS_BIND is the socket address and DNS_RELAY_IP is the
# literal every A answer carries — a hostname there made every A answer
# raise inside the responder (2026-09-07, whole-zone outage, NS still fine).
resolve_ipv4() {
    case "$1" in
    *[!0-9.]*) getent ahostsv4 "$1" 2>/dev/null | awk '{print $1; exit}' ;;
    *) printf '%s\n' "$1" ;;
    esac
}
HOST_IP=$(resolve_ipv4 "$HOST_NAME")
[ -n "$HOST_IP" ] || { echo "cannot resolve '$HOST_NAME' to an IPv4 address" >&2; exit 1; }
NODE_ID=${NODE_ID:-registry-ash-1}
# The IP every serve A answer resolves to: the serve FLOATING IP the raw
# tls-stream ingress binds on :443 (SERVE_BIND_IP in the box's
# /etc/autonomy-serve/serve.env), never the box's own IP — that address's
# :443 is the estate Caddy, which has no certificate for serve names and
# answers every ClientHello with a TLS alert. Defaulting to the box IP took
# every serve host down on 2026-09-07 while NS/SOA looked healthy. So: read
# the box's serve env unless --relay-ip is given explicitly.
if [ -z "$RELAY_IP" ]; then
    RELAY_IP=$(ssh -o IdentitiesOnly=yes "$HOST"         'sed -n "s/^SERVE_BIND_IP=//p" /etc/autonomy-serve/serve.env 2>/dev/null' | tr -d '"' | head -1)
    [ -n "$RELAY_IP" ] || {
        echo "no SERVE_BIND_IP in $HOST:/etc/autonomy-serve/serve.env — pass --relay-ip <serve floating IP> explicitly" >&2
        exit 1
    }
    echo "== relay IP from the box's serve env: $RELAY_IP"
fi
case "$RELAY_IP" in
*[!0-9.]*) echo "--relay-ip must be a dotted IPv4 address, got '$RELAY_IP'" >&2; exit 1 ;;
esac
if [ "$RELAY_IP" = "$HOST_IP" ]; then
    echo "refusing: relay IP $RELAY_IP is the box's own address; serve answers must point at the serve floating IP (SERVE_BIND_IP)" >&2
    exit 1
fi

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
DNS_RELAY_IP=$RELAY_IP
ENV
install -m 0644 /tmp/autonomy-registry-dns.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q autonomy-registry-dns
systemctl restart autonomy-registry-dns
EOF

# Prove an authoritative answer WITHOUT depending on dig being on the
# control host (it often isn't — a fresh box or a minimal container has no
# dnsutils). Use dig when present, else a stdlib-Python UDP query. A
# deploy's own success check must not hinge on an un-guaranteed tool.
probe_answer() {  # -> prints the answered A record, or nothing
    if command -v dig >/dev/null 2>&1; then
        dig +short +time=2 +tries=1 @"$HOST_IP" probe.serve.auto.network A \
            2>/dev/null | head -1
    else
        HOST_IP="$HOST_IP" python3 - <<'PY' 2>/dev/null
import os, socket, struct
ip = os.environ["HOST_IP"]
q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
for lbl in "probe.serve.auto.network".split("."):
    q += bytes([len(lbl)]) + lbl.encode()
q += b"\x00" + struct.pack(">HH", 1, 1)  # QTYPE A, IN
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
try:
    s.sendto(q, (ip, 53)); raw, _ = s.recvfrom(2048)
except Exception:
    raise SystemExit(0)
# Last 4 bytes of a single-A answer are the address; parse the answer RR.
if raw[6:8] != b"\x00\x01":  # ANCOUNT >= 1
    raise SystemExit(0)
print(".".join(str(b) for b in raw[-4:]))
PY
    fi
}

echo "== proving authoritative answers"
for _ in $(seq 15); do
    if [ "$(probe_answer)" = "$RELAY_IP" ]; then
        echo "   @$HOST_IP answers probe.serve.auto.network → $RELAY_IP ✓"
        echo "deploy complete. Next: verify-dns.sh $HOST_IP"
        exit 0
    fi
    sleep 2
done
echo "@$HOST_IP never answered probe.serve.auto.network A → $RELAY_IP (got '$(probe_answer)')." >&2
echo "This is a real outage signal, not a proof-script quirk: NS/SOA can answer while A fails." >&2
echo "Check: ssh $HOST 'cat /etc/autonomy-dns/dns.env; journalctl -u autonomy-registry-dns -n 30'" >&2
exit 1
