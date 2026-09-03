#!/usr/bin/env bash
# Bind the serve.auto.network floating IPv4 to the interface before the
# forwarder starts (Hetzner floating IPs are not auto-configured on the NIC).
# Inbound-only: no route/source changes (the primary IP keeps egress).
#
# Binds two ways on purpose: `ip address replace` takes effect immediately,
# and the netplan drop-in makes systemd-networkd itself durably aware of the
# address, so a LATER networkd restart (a routine unattended-upgrades
# daemon-reexec caused exactly this on 2026-09-03) reasserts it instead of
# silently wiping it. graph://f93ab508-212 has the full incident.
set -euo pipefail
: "${SERVE_BIND_IP:?SERVE_BIND_IP is required}"
: "${SERVE_INTERFACE:?SERVE_INTERFACE is required}"
case "$SERVE_BIND_IP" in
    5.161.17.217) ;;
    *) echo "refusing unexpected SERVE_BIND_IP: $SERVE_BIND_IP" >&2; exit 1 ;;
esac
ip address replace "$SERVE_BIND_IP/32" dev "$SERVE_INTERFACE"
ip -4 address show dev "$SERVE_INTERFACE" | grep -Fq "$SERVE_BIND_IP/32"

netplan_file=/etc/netplan/60-serve-floating-ip.yaml
cat >"$netplan_file" <<EOF
network:
  version: 2
  ethernets:
    $SERVE_INTERFACE:
      addresses:
      - "$SERVE_BIND_IP/32"
EOF
chmod 600 "$netplan_file"
netplan apply

# `netplan apply` briefly tears down and rebuilds the link's addresses/routes
# as it reconciles -- an immediate check can race that window. Poll instead
# of trusting the first read.
for _ in 1 2 3 4 5; do
    ip -4 address show dev "$SERVE_INTERFACE" | grep -Fq "$SERVE_BIND_IP/32" && break
    sleep 1
done
ip -4 address show dev "$SERVE_INTERFACE" | grep -Fq "$SERVE_BIND_IP/32"
