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
        #
        # Machine-readable event lines (auto-mu7qf) — the dashboard's
        # drill runner parses ONLY lines starting with '@':
        #   @snapshot <id>
        #   @check <name> <ok|fail> <detail...>
        #   @verdict <pass|fail>
        # Human lines are unchanged and interleave freely.
        TARGET="/tmp/autonomy-restore-drill-$(date +%s)"
        mkdir -p "$TARGET"
        # A 2.9 GB scratch restore must never outlive the drill, pass
        # or fail (it did on the 2026-09-06 false-negative FAIL).
        trap 'rm -rf "$TARGET"' EXIT

        SNAP_ID="$(restic snapshots latest --tag "kind=db" --json 2>/dev/null \
            | "$PYTHON" -c 'import json,sys
rows = json.load(sys.stdin) or []
print(rows[-1].get("short_id", "") if rows else "")' 2>/dev/null || true)"
        [[ -n "$SNAP_ID" ]] && echo "@snapshot ${SNAP_ID}"

        echo "drill: restoring latest db snapshot to ${TARGET}"
        if ! restic restore latest --tag "kind=db" --target "$TARGET"; then
            echo "@check restore fail restic restore failed"
            echo "@verdict fail"
            echo "drill: FAIL — restic restore failed" >&2
            exit 1
        fi
        echo "@check restore ok latest kind=db snapshot restored"

        MARKER="$(find "$TARGET" -name ".backup-complete" | head -1)"
        if [[ -z "$MARKER" ]]; then
            echo "@check marker fail no .backup-complete marker (pre-contract or partial capture)"
            echo "@verdict fail"
            echo "drill: FAIL — no .backup-complete marker (pre-contract or partial capture)" >&2
            exit 1
        fi
        MARKER_TEXT="$(tr '\n' ' ' < "$MARKER")"
        echo "drill: capture marker: ${MARKER_TEXT}"
        echo "@check marker ok ${MARKER_TEXT}"

        # Every restored SQLite DB must pass integrity_check. Through
        # python with the app-defined SQL functions registered — the
        # bare sqlite3 CLI cannot evaluate personal.db's
        # fleet_sha256_text() expression index and false-negatives.
        if ! INTEGRITY="$("$PYTHON" "${SCRIPT_DIR}/backup_stores.py" integrity "$TARGET")"; then
            echo "$INTEGRITY"
            echo "@check integrity fail $(printf '%s\n' "$INTEGRITY" | grep -m1 FAIL || echo integrity check failed)"
            echo "@verdict fail"
            echo "drill: FAIL — integrity check failed" >&2
            exit 1
        fi
        DB_COUNT="$(printf '%s\n' "$INTEGRITY" | grep -c $'\tok$' || true)"
        echo "drill: ${DB_COUNT} SQLite databases passed integrity_check"
        if [[ "$DB_COUNT" -lt 1 ]]; then
            echo "@check integrity fail no SQLite databases in restored snapshot"
            echo "@verdict fail"
            echo "drill: FAIL — no SQLite databases in restored snapshot" >&2
            exit 1
        fi
        echo "@check integrity ok ${DB_COUNT} databases passed integrity_check"

        # The primary org graph must look like real content, not a stub.
        AUTONOMY="$(find "$TARGET" -path "*/orgs/autonomy.db" | head -1)"
        if [[ -n "$AUTONOMY" ]]; then
            ROW="$(sqlite3 "$AUTONOMY" "SELECT COUNT(*) FROM sources" 2>/dev/null || echo 0)"
            echo "drill: orgs/autonomy.db sources=${ROW}"
            if [[ "$ROW" -lt 100 ]]; then
                echo "@check sources-sanity fail suspiciously small sources table (${ROW} rows)"
                echo "@verdict fail"
                echo "drill: FAIL — suspiciously small sources table (${ROW} rows)" >&2
                exit 1
            fi
            echo "@check sources-sanity ok orgs/autonomy.db sources=${ROW}"
        else
            echo "drill: WARN — orgs/autonomy.db not in snapshot (foreign deployment?)"
            echo "@check sources-sanity skipped orgs/autonomy.db not in snapshot"
        fi

        # Beads: the marker records how many dolt databases were dumped;
        # hold the snapshot to that number.
        EXPECTED_BEADS="$(sed -n 's/^beads_databases=//p' "$MARKER")"
        FOUND_BEADS="$(find "$TARGET" -path "*/beads/*.sql" | wc -l)"
        echo "drill: beads dumps found=${FOUND_BEADS} expected=${EXPECTED_BEADS:-?}"
        if [[ -n "$EXPECTED_BEADS" && "$FOUND_BEADS" -lt "$EXPECTED_BEADS" ]]; then
            echo "@check beads-count fail ${FOUND_BEADS} dumps, marker says ${EXPECTED_BEADS}"
            echo "@verdict fail"
            echo "drill: FAIL — snapshot has ${FOUND_BEADS} beads dumps, marker says ${EXPECTED_BEADS}" >&2
            exit 1
        fi
        echo "@check beads-count ok ${FOUND_BEADS}/${EXPECTED_BEADS:-?} dumps"

        echo "@verdict pass"
        echo "drill: PASS"
        ;;

    *)
        echo "Usage: $0 {list|restore|drill} [args...]" >&2
        exit 1
        ;;
esac
