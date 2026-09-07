#!/bin/sh
# Independent Uvicorn worker: deliberately no reload watcher.
set -eu
cd /app
if [ "${VOICE_SIDECAR_ENABLED:-false}" != true ]; then
    echo 'voice profile requires VOICE_SIDECAR_ENABLED=true' >&2
    exit 1
fi
if [ "${DASHBOARD_TLS:-}" = off ]; then
    exec python3 -m uvicorn tools.dashboard.voice_gateway:app --host 0.0.0.0 --port 8082
fi
exec python3 -m uvicorn tools.dashboard.voice_gateway:app \
    --host 0.0.0.0 --port 8082 \
    --ssl-certfile "${AUTONOMY_TLS_CERT:-/app/data/tls.crt}" \
    --ssl-keyfile "${AUTONOMY_TLS_KEY:-/app/data/tls.key}"
