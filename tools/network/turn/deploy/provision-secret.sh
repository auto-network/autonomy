#!/usr/bin/env bash
set -euo pipefail

DEST=${1:-/etc/autonomy-coturn/turn-rest-secrets}
if [ -e "$DEST" ]; then
    echo "refusing to replace existing TURN secret: $DEST" >&2
    exit 1
fi

install -d -m 0700 "$(dirname "$DEST")"
temporary="${DEST}.new.$$"
trap 'rm -f "$temporary"' EXIT
umask 077
openssl rand -out "$temporary" -hex 32
if ! grep -Eq '^[0-9a-f]{64}$' "$temporary"; then
    echo "generated TURN secret failed shape validation" >&2
    exit 1
fi
install -m 0600 -o root -g root "$temporary" "$DEST"
rm -f "$temporary"
trap - EXIT
echo "provisioned one root-only TURN REST secret at $DEST (value not printed)"
