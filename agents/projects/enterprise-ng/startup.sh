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

# job_framework needs pg_cron; pre-build a postgres:15 image with the
# extension so `task test-deps-up` (and manual compose runs) can use it.
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

if [ -f /workspace/enterprise_ng/pyproject.toml ]; then
    echo "[startup] poetry install (enterprise_ng)..."
    cd /workspace/enterprise_ng && poetry install --with=dev,python-tools,test --no-interaction || {
        echo "[startup] poetry install failed"; exit 1;
    }
fi

# Seed the legacy tables + db_version row. Non-fatal: if postgres is not
# up yet (agent has not run `task test-deps-up`), skip silently and the
# agent can re-run this step manually.
echo "[startup] seeding legacy tables..."
cd /workspace/enterprise_ng
poetry run python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from enterprise.common.db.entities.account import Account
from enterprise.common.db.entities.service import Service
from enterprise.common.db.entities.account_user import AccountUser
from sqlalchemy import text

async def seed():
    engine = create_async_engine('postgresql+asyncpg://postgres:postgres@localhost:5433/postgres')
    from enterprise.common.db.base import LegacyBase
    async with engine.begin() as conn:
        await conn.run_sync(LegacyBase.metadata.create_all)
        await conn.execute(text(\"\"\"INSERT INTO anchore (db_version) VALUES (6000) ON CONFLICT DO NOTHING\"\"\"))
    await engine.dispose()

asyncio.run(seed())
" 2>&1 || echo "[startup] legacy table seeding failed (non-fatal — run after task test-deps-up)"

echo "[startup] $(date -u +%FT%TZ) enterprise-ng startup complete"
