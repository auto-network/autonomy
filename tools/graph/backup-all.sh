#!/usr/bin/env bash
# Backup all databases: hourly (10 copies) and daily (7 copies).
#
# Crontab:
#   0 * * * * /home/jeremy/workspace/autonomy/tools/graph/backup-all.sh hourly
#   0 3 * * * /home/jeremy/workspace/autonomy/tools/graph/backup-all.sh daily

set -euo pipefail

SQLITE3="/home/jeremy/miniconda3/bin/sqlite3"
DOLT="/home/jeremy/go/bin/dolt"
ROOT="/home/jeremy/workspace/autonomy"
BACKUP_ROOT="${ROOT}/data/backups"

TIER="${1:-hourly}"
case "$TIER" in
    hourly) KEEP=10 ;;
    daily)  KEEP=7 ;;
    *)      echo "Usage: $0 {hourly|daily}" >&2; exit 1 ;;
esac

STAMP=$(date +%Y%m%d-%H%M%S)
DEST="${BACKUP_ROOT}/${TIER}/${STAMP}"
mkdir -p "$DEST"

# ── SQLite databases ──────────────────────────────────────────
backup_sqlite() {
    local name="$1" dbpath="$2"
    if [[ -f "$dbpath" ]]; then
        "$SQLITE3" "$dbpath" ".backup '${DEST}/${name}'"
        echo "  ${name}: $(du -h "${DEST}/${name}" | cut -f1)"
    else
        echo "  ${name}: SKIPPED (not found)"
    fi
}

backup_sqlite "graph.db"       "${ROOT}/data/graph.db"
backup_sqlite "dispatch.db"    "${ROOT}/data/dispatch.db"
backup_sqlite "experiments.db" "${ROOT}/data/experiments.db"
backup_sqlite "dashboard.db"   "${ROOT}/data/dashboard.db"
backup_sqlite "auth.db"        "${ROOT}/data/auth.db"

# ── Beads (dolt — mysqldump via docker against running sql-server) ─
DOLT_PORT=3306
if docker run --rm --network host mysql:8 mysqldump \
    --host=127.0.0.1 --port="${DOLT_PORT}" --user=root \
    --no-tablespaces --databases auto \
    > "${DEST}/beads.sql" 2>/dev/null; then
    echo "  beads: $(du -h "${DEST}/beads.sql" | cut -f1)"
else
    echo "  beads: FAILED (mysqldump error)"
    rm -f "${DEST}/beads.sql"
fi

# ── Prune old backups ─────────────────────────────────────────
TIER_DIR="${BACKUP_ROOT}/${TIER}"
ls -1dt "${TIER_DIR}"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf

echo "$(date -Iseconds) ${TIER} backup complete: ${DEST}"
