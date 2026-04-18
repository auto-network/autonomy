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

# Expose the Anchore Enterprise license (provided via /etc/autonomy/artifacts/)
# at the path enterprise_ng tooling expects — the filesystem root.
if [ -f /etc/autonomy/artifacts/license.yaml ]; then
    sudo ln -sf /etc/autonomy/artifacts/license.yaml /license.yaml
fi

# Stale config field that Pydantic now rejects — strip it from the environment
# before anything reads config.
unset ANCHORE_EXTERNAL_TLS

# job_framework requires pg_cron. The repo's compose file pulls plain
# postgres:15, so build a pgcron-enabled image and retag it as
# postgres:15 locally — compose finds the tag already present and skips
# the Docker Hub pull.
if ! docker image inspect postgres-pgcron:15 > /dev/null 2>&1; then
    echo "[startup] building postgres-pgcron:15..."
    cat > /tmp/Dockerfile.pgcron <<'PGEOF'
FROM postgres:15
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends postgresql-15-cron \
    && rm -rf /var/lib/apt/lists/* \
    && echo "shared_preload_libraries = 'pg_cron'" >> /usr/share/postgresql/postgresql.conf.sample
PGEOF
    docker build -t postgres-pgcron:15 -f /tmp/Dockerfile.pgcron /tmp
fi
docker tag postgres-pgcron:15 postgres:15

if [ -f /workspace/enterprise_ng/pyproject.toml ]; then
    echo "[startup] poetry install (enterprise_ng)..."
    cd /workspace/enterprise_ng && poetry install --with=dev,python-tools,test --no-interaction || {
        echo "[startup] poetry install failed"; exit 1;
    }
fi

echo "[startup] $(date -u +%FT%TZ) enterprise-ng startup complete"
