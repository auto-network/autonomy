#!/usr/bin/env bash
set -euo pipefail

: "${TURN_PUBLIC_IP:?TURN_PUBLIC_IP is required}"
: "${TURN_RELAY_MIN_PORT:?TURN_RELAY_MIN_PORT is required}"
: "${TURN_RELAY_MAX_PORT:?TURN_RELAY_MAX_PORT is required}"
[ "$TURN_PUBLIC_IP" != 5.161.219.195 ] || {
    echo "refusing to publish coturn on Caddy's public IPv4" >&2
    exit 1
}

IMAGE='coturn/coturn@sha256:75e9ebd1e19005bec0c7f591d29afe22f959916ac8d9c852452f27db8c789828'
TURN_RUNTIME_GID=$(getent group autonomy-coturn | cut -d: -f3)
test -n "$TURN_RUNTIME_GID"

exec /usr/bin/docker run --rm --name autonomy-coturn \
    --pull=never \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --tmpfs /var/lib/coturn:rw,nosuid,nodev,noexec,size=16m \
    --cap-drop=ALL \
    --cap-add=NET_BIND_SERVICE \
    --user "65534:${TURN_RUNTIME_GID}" \
    --security-opt no-new-privileges \
    --pids-limit 256 \
    --memory "${TURN_CONTAINER_MEMORY:-512m}" \
    --cpus "${TURN_CONTAINER_CPUS:-1.0}" \
    --log-driver journald \
    --label service=auto-network-coturn \
    --mount type=bind,src=/run/autonomy-coturn,dst=/etc/coturn,readonly \
    --publish "${TURN_PUBLIC_IP}:3478:3478/tcp" \
    --publish "${TURN_PUBLIC_IP}:3478:3478/udp" \
    --publish "${TURN_PUBLIC_IP}:443:5349/tcp" \
    --publish "${TURN_PUBLIC_IP}:${TURN_RELAY_MIN_PORT}-${TURN_RELAY_MAX_PORT}:${TURN_RELAY_MIN_PORT}-${TURN_RELAY_MAX_PORT}/udp" \
    --publish 127.0.0.1:9641:9641/tcp \
    "$IMAGE" \
    -c /etc/coturn/turnserver.conf
