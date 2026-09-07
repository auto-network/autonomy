#!/bin/sh
# The serving half of the entrypoint, run as the non-root "autonomy" user
# (uid 1000) after deploy/entrypoint.sh has prepared the volumes and dropped
# privileges with gosu. Runs as the SAME uid the session agents run as, so the
# worktrees, artifacts, and clones this process prepares are already owned by the
# identity the sessions use — no root-vs-agent permission gap.
set -e
cd /app

INIT_ARGS=""
if [ "${DASHBOARD_TLS:-}" = "off" ]; then
    INIT_ARGS="--no-tls"
fi

# Refuse a volume written by a newer node, then run every idempotent forward
# schema migration against the mounted volume (subsumes first-run init). Runs as
# autonomy, so the DBs and TLS keypair it creates are autonomy-owned.
python3 -m tools.portability migrate-on-mount /app/data $INIT_ARGS

SSL_ARGS=""
if [ -f "${AUTONOMY_TLS_CERT:-/app/data/tls.crt}" ] && [ -f "${AUTONOMY_TLS_KEY:-/app/data/tls.key}" ] && [ "${DASHBOARD_TLS:-}" != "off" ]; then
    SSL_ARGS="--ssl-certfile ${AUTONOMY_TLS_CERT:-/app/data/tls.crt} --ssl-keyfile ${AUTONOMY_TLS_KEY:-/app/data/tls.key}"
fi

# Rebuild CSS on template edits (best-effort, backgrounded, never fatal).
if [ -x /usr/local/bin/tailwindcss ]; then
    /usr/local/bin/tailwindcss --cwd /app/tools/dashboard \
        -i tailwind.input.css -o static/tailwind.css --watch=always \
        >/app/data/tailwind.log 2>&1 &
fi

# Hot-reload the code from the autonomy-code volume, same as the dev box.
export DASHBOARD_RESTART_TOKEN="${DASHBOARD_RESTART_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
exec python3 -m tools.dashboard.reload_with_notice tools.dashboard.server:app \
    --host "${DASHBOARD_HOST:-0.0.0.0}" \
    --port "${DASHBOARD_PORT:-8080}" \
    --reload \
    --reload-dir tools/dashboard \
    --reload-dir tools/graph \
    --reload-dir tools/network \
    --reload-dir agents \
    --reload-exclude 'tools/dashboard/tests/*' \
    --reload-exclude 'tools/graph/tests/*' \
    --reload-exclude 'agents/tests/*' \
    --reload-exclude '**/__pycache__/*' \
    --timeout-graceful-shutdown 5 \
    --no-access-log \
    $SSL_ARGS
