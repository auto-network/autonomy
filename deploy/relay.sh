#!/bin/sh
# MCP relay service (compose profile `relay` — see docker-compose.yml).
#
# The tunnel-client binary and its profile are OPERATOR DATA, not part of the
# image: data/services/mcp-relay/{tunnel-client,profiles/}. Its two
# credentials -- the control-plane API key and the relay's dashboard service
# token -- come ONLY from the vault (auto-5gdao): the dashboard opens the
# audited rows mcp-relay.control-plane-api-key and mcp-relay.service-token
# and releases them into the host's ramfs key cache, whose mcp-relay/
# subdirectory compose binds read-only at $SECRETS_DIR. No env file is read.
# Until the vault is unlocked (after a reboot the ramfs is empty) there is
# nothing to read: this waits, bounded, then exits with the reason, and
# docker's restart backoff retries -- never a tight restart storm.
set -eu

RELAY_DIR=/app/data/services/mcp-relay
SECRETS_DIR="${MCP_RELAY_SECRETS_DIR:-/run/mcp-relay-secrets}"
WAIT_S="${MCP_RELAY_SECRET_WAIT_S:-120}"

if [ ! -x "$RELAY_DIR/tunnel-client" ]; then
    echo "mcp-relay: $RELAY_DIR/tunnel-client missing or not executable —" \
         "this node has no relay provisioned; disable the 'relay' profile" \
         "or provision per DEPLOY.md" >&2
    exit 1
fi

n=0
while [ ! -s "$SECRETS_DIR/service-token" ] || [ ! -s "$SECRETS_DIR/control-plane-api-key" ]; do
    if [ "$n" -ge "$WAIT_S" ]; then
        echo "mcp-relay: credentials not released into $SECRETS_DIR after ${WAIT_S}s —" \
             "seal mcp-relay.service-token and mcp-relay.control-plane-api-key" \
             "(audited) and unlock the vault on the dashboard" >&2
        exit 1
    fi
    sleep 1
    n=$((n + 1))
done

# Read from the released files: the values are never on argv, never in a
# file on disk.
MCP_RELAY_SERVICE_TOKEN="$(cat "$SECRETS_DIR/service-token")"
CONTROL_PLANE_API_KEY="$(cat "$SECRETS_DIR/control-plane-api-key")"
export MCP_RELAY_SERVICE_TOKEN CONTROL_PLANE_API_KEY

exec "$RELAY_DIR/tunnel-client" run \
    --profile "${MCP_RELAY_PROFILE:-autonomy-relay}" \
    --profile-dir "$RELAY_DIR/profiles"
