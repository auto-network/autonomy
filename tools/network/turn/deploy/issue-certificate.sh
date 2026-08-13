#!/usr/bin/env bash
set -euo pipefail

: "${TURN_PUBLIC_IP:?set TURN_PUBLIC_IP to the approved second IPv4}"
: "${ACME_EMAIL:?set ACME_EMAIL for ACME expiry notices}"

resolved=$(getent ahostsv4 turn.auto.network | awk 'NR == 1 {print $1}')
if [ "$resolved" != "$TURN_PUBLIC_IP" ]; then
    echo "turn.auto.network resolves to '$resolved', expected '$TURN_PUBLIC_IP'" >&2
    exit 1
fi
if ss -ltnH "sport = :80" | awk -v ip="$TURN_PUBLIC_IP" '
    $4 == ip ":80" || $4 == "0.0.0.0:80" || $4 == "*:80" { found=1 }
    END { exit !found }
'; then
    echo "TCP 80 is already occupied; Caddy must bind only the original IP" >&2
    exit 1
fi

exec certbot certonly \
    --standalone \
    --non-interactive \
    --agree-tos \
    --email "$ACME_EMAIL" \
    --preferred-challenges http \
    --http-01-address "$TURN_PUBLIC_IP" \
    --http-01-port 80 \
    --deploy-hook 'systemctl try-restart autonomy-coturn.service' \
    -d turn.auto.network
