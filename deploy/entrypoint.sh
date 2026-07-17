#!/bin/sh
# Home-base container entrypoint: first-run init (H3), then the dashboard.
#
# Environment (all optional — see DEPLOY.md):
#   AUTONOMY_FIRST_ORG / AUTONOMY_FIRST_ORG_NAME  first org naming (read by tools.init)
#   DASHBOARD_HOST / DASHBOARD_PORT               bind address (default 0.0.0.0:8080)
#   DASHBOARD_TLS=off                             skip TLS keypair + serve plain HTTP
#   DASHBOARD_DOMAIN                              CN/SAN for the self-signed cert
set -e
cd /app

INIT_ARGS=""
if [ "${DASHBOARD_TLS:-}" = "off" ]; then
    INIT_ARGS="--no-tls"
fi

# Idempotent: an already-initialized data volume is a no-op (H3 contract).
python3 -m tools.init $INIT_ARGS

SSL_ARGS=""
if [ -f /app/data/tls.crt ] && [ -f /app/data/tls.key ] && [ "${DASHBOARD_TLS:-}" != "off" ]; then
    SSL_ARGS="--ssl-certfile /app/data/tls.crt --ssl-keyfile /app/data/tls.key"
fi

exec python3 -m uvicorn tools.dashboard.server:app \
    --host "${DASHBOARD_HOST:-0.0.0.0}" \
    --port "${DASHBOARD_PORT:-8080}" \
    $SSL_ARGS
