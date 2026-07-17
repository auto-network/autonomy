#!/usr/bin/env bash
# Backup all databases: hourly (10 copies) and daily (7 copies).
#
# Crontab (AUTONOMY_ROOT defaults to the repo this script lives in):
#   0 * * * * "$AUTONOMY_ROOT/tools/graph/backup-all.sh" hourly
#   0 3 * * * "$AUTONOMY_ROOT/tools/graph/backup-all.sh" daily

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AUTONOMY_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
SQLITE3="$(command -v sqlite3)"
DOLT="${DOLT_BIN:-$(command -v dolt || echo dolt)}"
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

# ── Org databases (post cross-org migration; real graph content) ──
mkdir -p "${DEST}/orgs"
shopt -s nullglob
for org_db in "${ROOT}/data/orgs"/*.db; do
    backup_sqlite "orgs/$(basename "$org_db")" "$org_db"
done
shopt -u nullglob

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

# ── Tail-call offsite push (architecture note graph://34507c98-af7) ───
# Idempotent — backup-offsite.sh exits 0 if agents/backup.env is absent.
# Failure is non-fatal: the local backup above is the source of truth.
if ! "${ROOT}/tools/graph/backup-offsite.sh" "$TIER"; then
    echo "$(date -Iseconds) WARN: ${TIER} offsite push failed (non-fatal)"
fi
