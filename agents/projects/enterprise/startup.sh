#!/bin/bash
# Enterprise project startup — runs inside the container in the background.
# The dind entrypoint wrapper (Dockerfile.dind) picks this up at /startup.sh,
# logs to /workspace/output/.setup.log, and records exit status in
# /workspace/output/.setup-exit.
#
# Steps: start dockerd (--tls=false + vfs storage for nested Docker safety),
# wait for it to accept connections, mark the enterprise repo clones as
# safe.directory for git, and run poetry install so the agent can import
# project deps without the first command paying install cost.
set -euo pipefail

echo "[startup] $(date -u +%FT%TZ) enterprise startup begin"

# ── 1. Start dockerd in the background ──────────────────────────
# --tls=false skips the 15s deprecation delay (src:3a627ab2-901).
# storage-driver=vfs is the safest choice for nested Docker.
sudo dockerd \
    --host=unix:///var/run/docker.sock \
    --data-root=/var/lib/docker \
    --storage-driver=vfs \
    --tls=false \
    > /tmp/dockerd.log 2>&1 &

# ── 2. Wait for dockerd to accept connections ───────────────────
echo "[startup] waiting for dockerd..."
for i in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
        echo "[startup] dockerd ready after ${i}s"
        break
    fi
    sleep 1
done
docker info >/dev/null 2>&1 || { echo "[startup] dockerd never became ready"; exit 1; }

# ── 3. git safe.directory for the mounted worktrees ─────────────
# Worktrees are owned by the host user; git refuses otherwise.
for d in /workspace/enterprise /workspace/enterprise_ng; do
    [ -d "$d" ] && git config --global --add safe.directory "$d"
done

# ── 4. Poetry install for enterprise ────────────────────────────
if [ -f /workspace/enterprise/pyproject.toml ]; then
    echo "[startup] poetry install (enterprise)..."
    cd /workspace/enterprise && poetry install --no-interaction || {
        echo "[startup] poetry install failed"; exit 1;
    }
fi

echo "[startup] $(date -u +%FT%TZ) enterprise startup complete"
