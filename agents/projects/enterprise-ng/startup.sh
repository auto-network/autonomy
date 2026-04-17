#!/bin/bash
# Enterprise NG project startup — runs inside the container in the background.
# Mirrors the enterprise pattern: start dockerd, wait for ready, git
# safe.directory, poetry install. See the enterprise startup.sh header
# comment for more detail and references.
set -euo pipefail

echo "[startup] $(date -u +%FT%TZ) enterprise-ng startup begin"

sudo dockerd \
    --host=unix:///var/run/docker.sock \
    --data-root=/var/lib/docker \
    --storage-driver=vfs \
    --tls=false \
    > /tmp/dockerd.log 2>&1 &

echo "[startup] waiting for dockerd..."
for i in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
        echo "[startup] dockerd ready after ${i}s"
        break
    fi
    sleep 1
done
docker info >/dev/null 2>&1 || { echo "[startup] dockerd never became ready"; exit 1; }

for d in /workspace/enterprise /workspace/enterprise_ng; do
    [ -d "$d" ] && git config --global --add safe.directory "$d"
done

if [ -f /workspace/enterprise_ng/pyproject.toml ]; then
    echo "[startup] poetry install (enterprise_ng)..."
    cd /workspace/enterprise_ng && poetry install --no-interaction || {
        echo "[startup] poetry install failed"; exit 1;
    }
fi

echo "[startup] $(date -u +%FT%TZ) enterprise-ng startup complete"
