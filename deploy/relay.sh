#!/bin/sh
# MCP relay service (compose profile `relay` — see docker-compose.yml).
#
# The tunnel-client binary and its credentials are OPERATOR DATA, not part
# of the image: data/services/mcp-relay/{tunnel-client,profiles/} plus an
# env file with the control-plane API key and the relay's dashboard service
# token. Canonical env file location is data/services/mcp-relay/relay.env;
# MCP_RELAY_ENV_FILE overrides it. A missing binary is a hard, explanatory
# failure rather than a restart loop with an empty log.
set -eu

RELAY_DIR=/app/data/services/mcp-relay
ENV_FILE="${MCP_RELAY_ENV_FILE:-$RELAY_DIR/relay.env}"

if [ ! -x "$RELAY_DIR/tunnel-client" ]; then
    echo "mcp-relay: $RELAY_DIR/tunnel-client missing or not executable —" \
         "this node has no relay provisioned; disable the 'relay' profile" \
         "or provision per DEPLOY.md" >&2
    exit 1
fi

if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

exec "$RELAY_DIR/tunnel-client" run \
    --profile "${MCP_RELAY_PROFILE:-autonomy-relay}" \
    --profile-dir "$RELAY_DIR/profiles"
