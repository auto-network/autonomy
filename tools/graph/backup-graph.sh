#!/usr/bin/env bash
# Hourly graph.db backup with rolling 10-copy buffer.
# Installed via crontab: 0 * * * * /home/jeremy/workspace/autonomy/tools/graph/backup-graph.sh

set -euo pipefail

DB="/home/jeremy/workspace/autonomy/data/graph.db"
BACKUP_DIR="/home/jeremy/workspace/autonomy/data"
PREFIX="graph.db.hourly-"
KEEP=10

# Skip if db doesn't exist
[[ -f "$DB" ]] || exit 0

STAMP=$(date +%Y%m%d-%H%M%S)
DEST="${BACKUP_DIR}/${PREFIX}${STAMP}"

# Use sqlite3 .backup for a consistent snapshot (safe even during writes)
/home/jeremy/miniconda3/bin/sqlite3 "$DB" ".backup '${DEST}'"

# Prune old hourly backups beyond the rolling buffer
ls -1t "${BACKUP_DIR}"/${PREFIX}* 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$(date -Iseconds) backup: ${DEST} ($(du -h "${DEST}" | cut -f1))"
