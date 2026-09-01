#!/usr/bin/env bash
# Deploy the delegated serve.auto.network authoritative pair (auto-g1jxw).
#
# Usage:
#   deploy.sh --primary root@<ip> --secondary root@<ip> [--relay-ip 5.161.219.195]
#
# Idempotent. Installs version-pinned PowerDNS Authoritative 4.9 from the
# official PowerDNS apt repository (signing key pinned by fingerprint
# below), renders role-specific configs, creates the zone on the primary
# (SOA/NS/glue/apex/wildcard → the relay ingress IP), configures the
# secondary as a native SECONDARY zone, installs the challenge broker +
# its purge timer on the primary, and proves each box answers
# authoritatively before returning. DNSSEC is explicitly unsigned; no DS
# is published anywhere.
#
# TLS/routing and the PARENT delegation are deliberately out of scope:
# the parent cutover is `namecheap_dns.py add-delegation` on the
# whitelisted host, operator-approved, AFTER verify-dns.sh passes in
# staging. See README.md.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

ZONE="serve.auto.network"
RELAY_IP="5.161.219.195"
PRIMARY="" SECONDARY=""
# PowerDNS repo signing key (repo.powerdns.com FD380FBB), pinned.
PDNS_KEY_FPR="D07E1C182D0E2CDE23A0D57ABE5DB7B8FD380FBB"
PDNS_SUITE="noble-auth-49"
PET_IPS="5.161.179.179"

while [ $# -gt 0 ]; do
    case $1 in
    --primary) PRIMARY=$2; shift 2 ;;
    --secondary) SECONDARY=$2; shift 2 ;;
    --relay-ip) RELAY_IP=$2; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done
[ -n "$PRIMARY" ] && [ -n "$SECONDARY" ] || {
    echo "usage: deploy.sh --primary root@ip --secondary root@ip" >&2; exit 1; }

for target in "$PRIMARY" "$SECONDARY"; do
    case "$target" in
    *auto-ash-1*|*5.161.179.179*)
        echo "refusing: '$target' is the legacy pet host" >&2; exit 1 ;;
    esac
done

PRIMARY_IP=${PRIMARY#*@}
SECONDARY_IP=${SECONDARY#*@}

ssh_run() { ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "$1" bash -s; }

install_pdns() {
    local target=$1
    echo "== $target: installing pinned PowerDNS Authoritative 4.9"
    ssh_run "$target" <<EOF
set -euo pipefail
install -d /etc/apt/keyrings
curl -fsSL https://repo.powerdns.com/FD380FBB-pub.asc \
    -o /etc/apt/keyrings/powerdns.asc
FPR=\$(gpg --show-keys --with-colons /etc/apt/keyrings/powerdns.asc \
    | awk -F: '/^fpr:/ {print \$10; exit}')
[ "\$FPR" = "$PDNS_KEY_FPR" ] || {
    echo "PowerDNS repo key fingerprint mismatch: \$FPR" >&2; exit 1; }
cat > /etc/apt/sources.list.d/powerdns.list <<'LIST'
deb [signed-by=/etc/apt/keyrings/powerdns.asc] http://repo.powerdns.com/ubuntu noble-auth-49 main
LIST
cat > /etc/apt/preferences.d/powerdns <<'PIN'
Package: pdns-*
Pin: origin repo.powerdns.com
Pin-Priority: 600
PIN
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    pdns-server pdns-backend-sqlite3 sqlite3 dnsutils >/dev/null
# Ubuntu ships a resolved stub on :53 sometimes — free the port.
if systemctl is-active -q systemd-resolved; then
    mkdir -p /etc/systemd/resolved.conf.d
    printf '[Resolve]\nDNSStubListener=no\n' \
        > /etc/systemd/resolved.conf.d/no-stub.conf
    ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
    systemctl restart systemd-resolved
fi
install -d -m 0755 /var/lib/powerdns
if [ ! -s /var/lib/powerdns/pdns.sqlite3 ]; then
    sqlite3 /var/lib/powerdns/pdns.sqlite3 \
        < /usr/share/pdns-backend-sqlite3/schema/schema.sqlite3.sql
fi
chown -R pdns:pdns /var/lib/powerdns
EOF
}

install_pdns "$PRIMARY"
install_pdns "$SECONDARY"

echo "== $PRIMARY: primary config + zone + broker"
ssh_run "$PRIMARY" <<EOF
set -euo pipefail
install -d -m 0755 /etc/autonomy-dns /var/lib/autonomy-dns
if [ ! -s /etc/autonomy-dns/api-key ]; then
    head -c 32 /dev/urandom | sha256sum | cut -d' ' -f1 \
        > /etc/autonomy-dns/api-key
fi
chmod 0600 /etc/autonomy-dns/api-key
cat > /etc/powerdns/pdns.conf <<CONF
launch=gsqlite3
gsqlite3-database=/var/lib/powerdns/pdns.sqlite3
local-address=0.0.0.0
primary=yes
api=yes
api-key=\$(cat /etc/autonomy-dns/api-key)
webserver=yes
webserver-address=127.0.0.1
webserver-port=8081
webserver-allow-from=127.0.0.1/32
allow-axfr-ips=$SECONDARY_IP/32
also-notify=$SECONDARY_IP
disable-syslog=no
security-poll-suffix=
CONF
systemctl enable -q pdns
systemctl restart pdns
if ! pdnsutil list-zone $ZONE >/dev/null 2>&1; then
    pdnsutil create-zone $ZONE ns1.$ZONE
    pdnsutil replace-rrset $ZONE @ SOA 3600 \
        "ns1.$ZONE. hostmaster.auto.network. 1 300 60 604800 300"
    pdnsutil replace-rrset $ZONE @ NS 3600 "ns1.$ZONE." "ns2.$ZONE."
    pdnsutil replace-rrset $ZONE ns1 A 3600 "$PRIMARY_IP"
    pdnsutil replace-rrset $ZONE ns2 A 3600 "$SECONDARY_IP"
    pdnsutil replace-rrset $ZONE @ A 300 "$RELAY_IP"
    pdnsutil replace-rrset $ZONE '*' A 300 "$RELAY_IP"
    pdnsutil increase-serial $ZONE
fi
EOF

scp -o IdentitiesOnly=yes -q \
    challenge_broker.py autonomy-dns-purge.service autonomy-dns-purge.timer \
    "$PRIMARY":/tmp/
ssh_run "$PRIMARY" <<'EOF'
set -euo pipefail
install -m 0755 /tmp/challenge_broker.py /usr/local/bin/challenge_broker.py
install -m 0644 /tmp/autonomy-dns-purge.service \
    /tmp/autonomy-dns-purge.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now autonomy-dns-purge.timer
EOF

echo "== $SECONDARY: secondary config + zone"
ssh_run "$SECONDARY" <<EOF
set -euo pipefail
cat > /etc/powerdns/pdns.conf <<CONF
launch=gsqlite3
gsqlite3-database=/var/lib/powerdns/pdns.sqlite3
local-address=0.0.0.0
secondary=yes
allow-notify-from=$PRIMARY_IP/32
disable-syslog=no
security-poll-suffix=
CONF
systemctl enable -q pdns
systemctl restart pdns
if ! pdnsutil list-zone $ZONE >/dev/null 2>&1; then
    pdnsutil create-secondary-zone $ZONE "$PRIMARY_IP"
fi
pdns_control retrieve $ZONE || true
EOF

echo "== proving authoritative answers"
for pair in "$PRIMARY_IP primary" "$SECONDARY_IP secondary"; do
    set -- $pair
    for _ in $(seq 30); do
        if dig +short +time=2 +tries=1 @"$1" "probe.$ZONE" A \
                | grep -q "$RELAY_IP"; then
            echo "   $2 @$1 answers probe.$ZONE → $RELAY_IP ✓"
            continue 2
        fi
        sleep 2
    done
    echo "$2 @$1 never answered probe.$ZONE" >&2
    exit 1
done
echo "deploy complete. Next: verify-dns.sh $PRIMARY_IP $SECONDARY_IP"
