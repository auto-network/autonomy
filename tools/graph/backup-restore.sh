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
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON="${REPO_ROOT}/.venv/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

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
        # A 2.9 GB scratch restore must never outlive the drill, pass
        # or fail (it did on the 2026-09-06 false-negative FAIL).
        trap 'rm -rf "$TARGET"' EXIT
        echo "drill: restoring latest db snapshot to ${TARGET}"
        restic restore latest --tag "kind=db" --target "$TARGET"

        MARKER="$(find "$TARGET" -name ".backup-complete" | head -1)"
        if [[ -z "$MARKER" ]]; then
            echo "drill: FAIL — no .backup-complete marker (pre-contract or partial capture)" >&2
            exit 1
        fi
        echo "drill: capture marker: $(tr '\n' ' ' < "$MARKER")"

        # Every restored SQLite DB must pass integrity_check. Through
        # python with the app-defined SQL functions registered — the
        # bare sqlite3 CLI cannot evaluate personal.db's
        # fleet_sha256_text() expression index and false-negatives.
        if ! INTEGRITY="$("$PYTHON" "${SCRIPT_DIR}/backup_stores.py" integrity "$TARGET")"; then
            echo "$INTEGRITY"
            echo "drill: FAIL — integrity check failed" >&2
            exit 1
        fi
        DB_COUNT="$(printf '%s\n' "$INTEGRITY" | grep -c $'\tok$' || true)"
        echo "drill: ${DB_COUNT} SQLite databases passed integrity_check"
        if [[ "$DB_COUNT" -lt 1 ]]; then
            echo "drill: FAIL — no SQLite databases in restored snapshot" >&2
            exit 1
        fi

        # The primary org graph must look like real content, not a stub.
        AUTONOMY="$(find "$TARGET" -path "*/orgs/autonomy.db" | head -1)"
        if [[ -n "$AUTONOMY" ]]; then
            ROW="$(sqlite3 "$AUTONOMY" "SELECT COUNT(*) FROM sources" 2>/dev/null || echo 0)"
            echo "drill: orgs/autonomy.db sources=${ROW}"
            if [[ "$ROW" -lt 100 ]]; then
                echo "drill: FAIL — suspiciously small sources table (${ROW} rows)" >&2
                exit 1
            fi
        else
            echo "drill: WARN — orgs/autonomy.db not in snapshot (foreign deployment?)"
        fi

        # Beads: the marker records how many dolt databases were dumped;
        # hold the snapshot to that number.
        EXPECTED_BEADS="$(sed -n 's/^beads_databases=//p' "$MARKER")"
        FOUND_BEADS="$(find "$TARGET" -path "*/beads/*.sql" | wc -l)"
        echo "drill: beads dumps found=${FOUND_BEADS} expected=${EXPECTED_BEADS:-?}"
        if [[ -n "$EXPECTED_BEADS" && "$FOUND_BEADS" -lt "$EXPECTED_BEADS" ]]; then
            echo "drill: FAIL — snapshot has ${FOUND_BEADS} beads dumps, marker says ${EXPECTED_BEADS}" >&2
            exit 1
        fi

        echo "drill: PASS"
        ;;

    *)
        echo "Usage: $0 {list|restore|drill} [args...]" >&2
        exit 1
        ;;
esac
