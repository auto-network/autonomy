#!/usr/bin/env bash
# Reproducibly bind the approved TURN floating IPv4 before coturn starts.
#
# Binds the address two ways on purpose: `ip address replace` takes effect
# immediately, and the netplan drop-in makes systemd-networkd itself durably
# aware of it, so a LATER networkd restart (a routine unattended-upgrades
# daemon-reexec caused exactly this on 2026-09-03, on this same box, for the
# serve floating IP) reasserts it instead of silently wiping it.
# graph://f93ab508-212 has the full incident. The routes below are left
# purely imperative -- confirmed they still resolve correctly after a
# `netplan apply` (DHCP's own default route already matches), but they are
# not themselves part of this durability fix.
set -euo pipefail

: "${TURN_PUBLIC_IP:?TURN_PUBLIC_IP is required}"
: "${TURN_INTERFACE:?TURN_INTERFACE is required}"

case "$TURN_PUBLIC_IP" in
    5.161.16.159) ;;
    *) echo "refusing unexpected TURN_PUBLIC_IP: $TURN_PUBLIC_IP" >&2; exit 1 ;;
esac

ip address replace "$TURN_PUBLIC_IP/32" dev "$TURN_INTERFACE"
ip route replace "${TURN_PUBLIC_IP}/32" dev "$TURN_INTERFACE" scope link
ip route replace default via "$TURN_GATEWAY" dev "$TURN_INTERFACE" src "$TURN_PRIMARY_IP" metric 100

ip -4 address show dev "$TURN_INTERFACE" | grep -Fq "$TURN_PUBLIC_IP/32"
ip -4 route get 1.1.1.1 | grep -Fq "src $TURN_PRIMARY_IP"

netplan_file=/etc/netplan/61-turn-floating-ip.yaml
cat >"$netplan_file" <<EOF
network:
  version: 2
  ethernets:
    $TURN_INTERFACE:
      addresses:
      - "$TURN_PUBLIC_IP/32"
EOF
chmod 600 "$netplan_file"
netplan apply

# `netplan apply` briefly tears down and rebuilds the link's addresses/routes
# as it reconciles -- an immediate check can race that window. Reassert the
# imperative route (cheap, idempotent) and poll instead of trusting the first
# read.
for _ in 1 2 3 4 5; do
    ip route replace default via "$TURN_GATEWAY" dev "$TURN_INTERFACE" src "$TURN_PRIMARY_IP" metric 100 2>/dev/null || true
    ip -4 address show dev "$TURN_INTERFACE" | grep -Fq "$TURN_PUBLIC_IP/32" \
        && ip -4 route get 1.1.1.1 2>/dev/null | grep -Fq "src $TURN_PRIMARY_IP" && break
    sleep 1
done
ip -4 address show dev "$TURN_INTERFACE" | grep -Fq "$TURN_PUBLIC_IP/32"
ip -4 route get 1.1.1.1 | grep -Fq "src $TURN_PRIMARY_IP"
