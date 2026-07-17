#!/usr/bin/env bash
# Hourly graph.db backup with rolling 10-copy buffer.
# Installed via crontab: 0 * * * * "$AUTONOMY_ROOT/tools/graph/backup-graph.sh"
# (AUTONOMY_ROOT defaults to the repo this script lives in.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AUTONOMY_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
DB="${ROOT}/data/graph.db"
BACKUP_DIR="${ROOT}/data"
PREFIX="graph.db.hourly-"
KEEP=10

# Skip if db doesn't exist
[[ -f "$DB" ]] || exit 0

STAMP=$(date +%Y%m%d-%H%M%S)
DEST="${BACKUP_DIR}/${PREFIX}${STAMP}"

# Use sqlite3 .backup for a consistent snapshot (safe even during writes)
sqlite3 "$DB" ".backup '${DEST}'"

# Prune old hourly backups beyond the rolling buffer
ls -1t "${BACKUP_DIR}"/${PREFIX}* 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$(date -Iseconds) backup: ${DEST} ($(du -h "${DEST}" | cut -f1))"
