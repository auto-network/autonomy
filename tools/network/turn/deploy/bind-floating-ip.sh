#!/usr/bin/env bash
# Reproducibly bind the approved TURN floating IPv4 before coturn starts.
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
