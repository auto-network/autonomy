#!/usr/bin/env bash
# Restore helper — pulls snapshots from the offsite restic repo.
# Defaults to a dry-run listing so "oops" commands don't overwrite live data.
#
# Usage:
#   tools/graph/backup-restore.sh list [--tier hourly|daily] [--kind db|data|claude|crontab]
#   tools/graph/backup-restore.sh restore <snapshot-id> <target-dir>
#   tools/graph/backup-restore.sh drill
#       — restore latest to /tmp/autonomy-restore-drill/, sanity-check DBs, discard.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/backup-env.sh"

CMD="${1:-list}"
shift || true

case "$CMD" in
    list)
        FILTER=()
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --tier) FILTER+=(--tag "tier=$2"); shift 2 ;;
                --kind) FILTER+=(--tag "kind=$2"); shift 2 ;;
                *) echo "unknown: $1" >&2; exit 1 ;;
            esac
        done
        restic snapshots "${FILTER[@]}"
        ;;

    restore)
        SNAP="${1:?snapshot id required}"
        TARGET="${2:?target dir required}"
        mkdir -p "$TARGET"
        restic restore "$SNAP" --target "$TARGET"
        echo "restored to: $TARGET"
        ;;

    drill)
        # Pull the latest db-kind snapshot into a scratch dir and run sanity
        # checks.  Fails loudly if any DB is corrupt or unreadable.
        TARGET="/tmp/autonomy-restore-drill-$(date +%s)"
        mkdir -p "$TARGET"
        echo "drill: restoring latest db snapshot to ${TARGET}"
        restic restore latest --tag "kind=db" --target "$TARGET"

        FOUND="$(find "$TARGET" -name "graph.db" | head -1)"
        if [[ -z "$FOUND" ]]; then
            echo "drill: FAIL — graph.db not found in restored snapshot" >&2
            exit 1
        fi

        echo "drill: sqlite integrity check on $(basename "$FOUND")"
        if ! sqlite3 "$FOUND" "PRAGMA integrity_check" | grep -q "^ok$"; then
            echo "drill: FAIL — graph.db integrity check failed" >&2
            exit 1
        fi

        ROW="$(sqlite3 "$FOUND" "SELECT COUNT(*) FROM sources" 2>/dev/null || echo 0)"
        echo "drill: graph.db sources=${ROW}"
        if [[ "$ROW" -lt 100 ]]; then
            echo "drill: FAIL — suspiciously small sources table (${ROW} rows)" >&2
            exit 1
        fi

        BEADS="$(find "$TARGET" -name "beads.sql" | head -1)"
        if [[ -n "$BEADS" ]]; then
            echo "drill: beads.sql present, size=$(du -h "$BEADS" | cut -f1)"
        else
            echo "drill: WARN — beads.sql missing from snapshot"
        fi

        echo "drill: PASS — cleaning up $TARGET"
        rm -rf "$TARGET"
        ;;

    *)
        echo "Usage: $0 {list|restore|drill} [args...]" >&2
        exit 1
        ;;
esac
