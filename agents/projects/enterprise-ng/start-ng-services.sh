#!/bin/bash
# Boot Enterprise NG test dependencies and the component_catalog service.
#
# On-demand helper installed to /etc/autonomy/bin/start-ng-services by
# startup.sh. Safe to re-run: compose up is idempotent, database creation
# is conditional, and component_catalog is only started if not already
# backgrounded.
set -euo pipefail

log() { echo "[start-ng-services] $(date -u +%FT%TZ) $*"; }

COMPOSE=/workspace/enterprise_ng/tests/integration/deps/docker-compose.yaml
DEPS_PG=enterprise_ng_test_db
PIDFILE=/workspace/output/component_catalog.pid
LOGFILE=/workspace/output/component_catalog.log

if [ ! -f "$COMPOSE" ]; then
    echo "ERROR: missing compose file at $COMPOSE" >&2
    exit 1
fi

log "bringing up test deps..."
cd /workspace/enterprise_ng
docker compose -f "$COMPOSE" up -d

log "waiting for postgres ($DEPS_PG)..."
for i in $(seq 1 60); do
    if docker exec "$DEPS_PG" pg_isready -U postgres >/dev/null 2>&1; then
        log "postgres ready after ${i}s"
        break
    fi
    sleep 1
done
docker exec "$DEPS_PG" pg_isready -U postgres >/dev/null 2>&1 \
    || { echo "ERROR: postgres never became ready" >&2; exit 1; }

log "ensuring ng_test database exists..."
if ! docker exec "$DEPS_PG" psql -U postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname='ng_test'" | grep -q 1; then
    docker exec "$DEPS_PG" psql -U postgres -c 'CREATE DATABASE ng_test;'
    log "created ng_test"
else
    log "ng_test already present"
fi

log "poetry run ng-enterprise db upgrade..."
poetry run ng-enterprise db upgrade

mkdir -p /workspace/output

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    log "component_catalog already running (pid=$(cat "$PIDFILE")). log=$LOGFILE"
else
    log "starting component_catalog (backgrounded)..."
    nohup poetry run ng-enterprise service start component_catalog \
        > "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    log "component_catalog pid=$(cat "$PIDFILE"). log=$LOGFILE"
fi

log "done."
