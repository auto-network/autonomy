#!/bin/sh
# Home-base container entrypoint: first-run init (H3), then the dashboard.
#
# Environment (all optional — see DEPLOY.md):
#   AUTONOMY_FIRST_ORG / AUTONOMY_FIRST_ORG_NAME  first org naming (read by tools.init)
#   AUTONOMY_INVITE                               join an EXISTING org instead of
#                                                 founding one (read by tools.init;
#                                                 mutually exclusive with FIRST_ORG)
#   DASHBOARD_HOST / DASHBOARD_PORT               bind address (default 0.0.0.0:8080)
#   DASHBOARD_TLS=off                             skip TLS keypair + serve plain HTTP
#   DASHBOARD_DOMAIN                              CN/SAN for the self-signed cert
set -e
cd /app

INIT_ARGS=""
if [ "${DASHBOARD_TLS:-}" = "off" ]; then
    INIT_ARGS="--no-tls"
fi

# Refuse a volume written by a newer node before any initializer mutates it,
# then run every idempotent forward schema migration against the mounted
# volume. This subsumes first-run init for an empty volume.
python3 -m tools.portability migrate-on-mount /app/data $INIT_ARGS

# Org-mount root (the autonomy-orgs volume mounts here). Ensure it exists even
# on a plain `docker run` without the volume, so provisioning and the hybrid
# resolver (auto-fteke) have a root to create orgs/<org>/ subdirs under. The
# node process is root, so it can read/write here; org subdirs are made on
# demand, not up front.
mkdir -p /app/orgs

# In-memory (ramfs) secret stores, provisioned before serving: per-session
# secret delivery and the dashboard key cache. Automatic via the Docker socket
# on a containerized node (a one-shot privileged helper mounts ramfs in the
# host mount namespace — no CAP_SYS_ADMIN on this container); idempotent every
# boot; ramfs is ephemeral across a host reboot, so re-checking each boot is
# what self-heals. Best-effort and LOUD on failure: the vault re-checks the
# filesystem class on every write and fails closed, so a provisioning miss
# degrades the secret features rather than taking the whole node down.
python3 -m agents.secret_ramfs || \
    echo "WARNING: secret ramfs provisioning failed — secret delivery and the key cache will fail closed until resolved" >&2

SSL_ARGS=""
if [ -f ${AUTONOMY_TLS_CERT:-/app/data/tls.crt} ] && [ -f ${AUTONOMY_TLS_KEY:-/app/data/tls.key} ] && [ "${DASHBOARD_TLS:-}" != "off" ]; then
    SSL_ARGS="--ssl-certfile ${AUTONOMY_TLS_CERT:-/app/data/tls.crt} --ssl-keyfile ${AUTONOMY_TLS_KEY:-/app/data/tls.key}"
fi

# Rebuild CSS on template edits, so a live code update renders fully — the same
# tailwind --watch the dev launcher (tools/dashboard/start-dashboard.sh) runs.
# Best-effort: a missing binary or a tailwind failure must never take the node
# down, so it is backgrounded and its exit is ignored.
if [ -x /usr/local/bin/tailwindcss ]; then
    /usr/local/bin/tailwindcss --cwd /app/tools/dashboard \
        -i tailwind.input.css -o static/tailwind.css --watch=always \
        >/app/data/tailwind.log 2>&1 &
fi

# Hot-reload the code from the autonomy-code volume, exactly like the dev box:
# an operator's in-place code update takes effect live, with no image rebuild
# and no restart. Same reload dirs/excludes as start-dashboard.sh; uvicorn 0.44
# uses its StatReload backend (no watchfiles needed — the dev box runs the same
# way). This replaces the previous static, no-reload invocation.
exec python3 -m uvicorn tools.dashboard.server:app \
    --host "${DASHBOARD_HOST:-0.0.0.0}" \
    --port "${DASHBOARD_PORT:-8080}" \
    --reload \
    --reload-dir tools/dashboard \
    --reload-dir tools/graph \
    --reload-dir agents \
    --reload-exclude 'tools/dashboard/tests/*' \
    --reload-exclude 'tools/graph/tests/*' \
    --reload-exclude 'agents/tests/*' \
    --reload-exclude '**/__pycache__/*' \
    --timeout-graceful-shutdown 5 \
    $SSL_ARGS
