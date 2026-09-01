#!/usr/bin/env bash
# Bind the serve.auto.network floating IPv4 to the interface before the
# forwarder starts (Hetzner floating IPs are not auto-configured on the NIC).
# Inbound-only: no route/source changes (the primary IP keeps egress).
set -euo pipefail
: "${SERVE_BIND_IP:?SERVE_BIND_IP is required}"
: "${SERVE_INTERFACE:?SERVE_INTERFACE is required}"
case "$SERVE_BIND_IP" in
    5.161.17.217) ;;
    *) echo "refusing unexpected SERVE_BIND_IP: $SERVE_BIND_IP" >&2; exit 1 ;;
esac
ip address replace "$SERVE_BIND_IP/32" dev "$SERVE_INTERFACE"
ip -4 address show dev "$SERVE_INTERFACE" | grep -Fq "$SERVE_BIND_IP/32"
