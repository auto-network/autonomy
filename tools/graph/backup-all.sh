#!/usr/bin/env bash
# Backup every persistent store to the backup root: hourly (10) and daily (7).
#
# Data-root contract (auto-iwct5 — the 2026-09-06 silent-no-op incident):
# every path resolves through tools/data_paths.py's STORE_MANIFEST via the
# backup_stores.py helper, honoring the store's own env var, then
# AUTONOMY_DATA_ROOT, then (legacy fallback only) this checkout's data/.
# This script must never re-derive a store path from its own location.
#
# The backup root (AUTONOMY_BACKUP_ROOT, default <data root>/backups) must
# ALREADY EXIST — on the host it is a symlink onto the NAS mount, and a
# missing mount must fail loudly, never silently write to local disk.
#
# Crontab (host: /opt/autonomy/data/backups -> /mnt/datapool/autonomy/backups):
#   AUTONOMY_DATA_ROOT=/opt/autonomy/data
#   0 * * * * "$AUTONOMY_ROOT/tools/graph/backup-all.sh" hourly
#   0 3 * * * "$AUTONOMY_ROOT/tools/graph/backup-all.sh" daily
#
# Beads (dolt) connections resolve like the DAO: DOLT_SQL_HOST/PORT/USER/
# PASSWORD env, else each beads dir's config.yaml/credentials.env, else
# 127.0.0.1:3306 root. Every provisioned database is dumped.
#
# Exit: 0 = complete incl. offsite; 1 = capture failed (NO offsite push,
# no prune, dir renamed *-FAILED); 2 = capture complete but offsite failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AUTONOMY_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
SQLITE3="$(command -v sqlite3)" || { echo "FATAL: sqlite3 not found" >&2; exit 1; }
PYTHON="${ROOT}/.venv/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)" || { echo "FATAL: python3 not found" >&2; exit 1; }
STORES_HELPER="${SCRIPT_DIR}/backup_stores.py"

DATA_ROOT="${AUTONOMY_DATA_ROOT:-${ROOT}/data}"
BACKUP_ROOT="${AUTONOMY_BACKUP_ROOT:-${DATA_ROOT}/backups}"

TIER="${1:-hourly}"
case "$TIER" in
    hourly) KEEP=10 ;;
    daily)  KEEP=7 ;;
    *)      echo "Usage: $0 {hourly|daily}" >&2; exit 1 ;;
esac

# ── Destination guard: never create the backup root ───────────────────
if [[ ! -d "$BACKUP_ROOT" ]]; then
    echo "FATAL: backup root ${BACKUP_ROOT} does not exist (NAS mount down or" \
         "symlink dangling?) — refusing to create it or write elsewhere" >&2
    exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
DEST="${BACKUP_ROOT}/${TIER}/${STAMP}"
umask 077   # key material lands in here; nothing needs to be group-readable
mkdir -p "$DEST"

FAILURES=0
STORE_COUNT=0
BEADS_COUNT=0

fail() {
    echo "  $*" >&2
    FAILURES=$((FAILURES + 1))
}

backup_sqlite() {
    # $1 = destination path relative to $DEST, $2 = source db path
    local rel="$1" dbpath="$2"
    mkdir -p "$(dirname "${DEST}/${rel}")"
    if "$SQLITE3" "$dbpath" ".backup '${DEST}/${rel}'"; then
        # stat, not du: on the NFS backup root du reports allocated
        # blocks before the NAS flushes (everything looked like "512").
        echo "  ${rel}: $(stat -c %s "${DEST}/${rel}") bytes"
        STORE_COUNT=$((STORE_COUNT + 1))
    else
        fail "${rel}: FAILED (sqlite .backup error from ${dbpath})"
    fi
}

backup_copy() {
    local rel="$1" src="$2"
    mkdir -p "$(dirname "${DEST}/${rel}")"
    if cp -a "$src" "${DEST}/${rel}"; then
        echo "  ${rel}: copied"
        STORE_COUNT=$((STORE_COUNT + 1))
    else
        fail "${rel}: FAILED (copy error from ${src})"
    fi
}

# ── Manifest stores ───────────────────────────────────────────────────
MANIFEST="$("$PYTHON" "$STORES_HELPER" stores)" || {
    echo "FATAL: could not enumerate the store manifest — refusing to guess" >&2
    exit 1
}
[[ -n "$MANIFEST" ]] || { echo "FATAL: empty store manifest" >&2; exit 1; }

echo "$(date -Iseconds) ${TIER} backup starting: data root ${DATA_ROOT} -> ${DEST}"

while IFS=$'\t' read -r key kind action required rel resolved; do
    if [[ ! -e "$resolved" ]]; then
        if [[ "$required" == "required" ]]; then
            fail "${key}: MISSING required store (${resolved})"
        else
            echo "  ${key}: absent (optional)"
        fi
        continue
    fi
    case "$action" in
        sqlite)
            backup_sqlite "$rel" "$resolved" ;;
        copy)
            backup_copy "$rel" "$resolved" ;;
        orgs-sqlite)
            shopt -s nullglob
            org_dbs=("$resolved"/*.db)
            shopt -u nullglob
            if [[ ${#org_dbs[@]} -eq 0 ]]; then
                fail "${key}: MISSING — ${resolved} contains no *.db"
                continue
            fi
            for org_db in "${org_dbs[@]}"; do
                backup_sqlite "${rel}/$(basename "$org_db")" "$org_db"
            done ;;
        verify)
            echo "  ${key}: present (content captured by offsite kind=data)"
            STORE_COUNT=$((STORE_COUNT + 1)) ;;
        *)
            fail "${key}: unknown backup action '${action}'" ;;
    esac
done <<< "$MANIFEST"

# ── Off-manifest databases (drift defense — see backup_stores.py) ─────
while IFS= read -r extra_db; do
    [[ -n "$extra_db" ]] || continue
    echo "  note: ${extra_db} is not in STORE_MANIFEST — backing it up anyway"
    backup_sqlite "$(basename "$extra_db")" "$extra_db"
done < <("$PYTHON" "$STORES_HELPER" extra-dbs)

# ── Beads (dolt) — every provisioned database ─────────────────────────
BEADS_ROOT="${BEADS_DIR:-${DATA_ROOT}/.beads}"
if [[ -d "$BEADS_ROOT" ]]; then
    if command -v mysqldump >/dev/null 2>&1; then
        MYSQLDUMP=(mysqldump)
    elif command -v docker >/dev/null 2>&1; then
        MYSQLDUMP=(docker run --rm --network host -e MYSQL_PWD mysql:8 mysqldump)
    else
        MYSQLDUMP=()
        fail "beads: FAILED (no mysqldump and no docker available)"
    fi
    BEADS_ROWS="$(BEADS_DIR="$BEADS_ROOT" "$PYTHON" "$STORES_HELPER" beads)" || {
        BEADS_ROWS=""
        fail "beads: FAILED (could not enumerate dolt databases)"
    }
    if [[ ${#MYSQLDUMP[@]} -gt 0 && -n "$BEADS_ROWS" ]]; then
        mkdir -p "${DEST}/beads"
        while IFS=$'\t' read -r db host port user password; do
            [[ -n "$db" ]] || continue
            # blocked_issues/ready_issues are views mysqldump cannot stat on
            # dolt (verified: exit 0 with them ignored, auto-iwct5).
            if MYSQL_PWD="$password" "${MYSQLDUMP[@]}" \
                --host="$host" --port="$port" --user="$user" \
                --no-tablespaces \
                --ignore-table="${db}.blocked_issues" \
                --ignore-table="${db}.ready_issues" \
                --databases "$db" \
                > "${DEST}/beads/${db}.sql" 2>"${DEST}/beads/${db}.err"; then
                rm -f "${DEST}/beads/${db}.err"
                echo "  beads/${db}.sql: $(stat -c %s "${DEST}/beads/${db}.sql") bytes"
                BEADS_COUNT=$((BEADS_COUNT + 1))
            else
                fail "beads/${db}: FAILED ($(head -1 "${DEST}/beads/${db}.err" 2>/dev/null || echo mysqldump error))"
                rm -f "${DEST}/beads/${db}.sql"
            fi
        done <<< "$BEADS_ROWS"
    fi
else
    echo "  beads: no ${BEADS_ROOT} — deployment has no beads tracker (skipped)"
fi

# ── Verdict ───────────────────────────────────────────────────────────
if [[ $FAILURES -gt 0 ]]; then
    echo "$(date -Iseconds) ${TIER} backup FAILED: ${FAILURES} store(s) missing or errored — NO offsite push" >&2
    mv "$DEST" "${DEST}-FAILED" 2>/dev/null || true
    exit 1
fi

{
    echo "completed_at=$(date -Iseconds)"
    echo "tier=${TIER}"
    echo "data_root=${DATA_ROOT}"
    echo "stores=${STORE_COUNT}"
    echo "beads_databases=${BEADS_COUNT}"
} > "${DEST}/.backup-complete"

# ── Prune old backups (successful runs only) ──────────────────────────
TIER_DIR="${BACKUP_ROOT}/${TIER}"
ls -1dt "${TIER_DIR}"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf

echo "$(date -Iseconds) ${TIER} backup complete: ${DEST} (${STORE_COUNT} stores, ${BEADS_COUNT} beads dbs)"

# ── Offsite push (architecture note graph://34507c98-af7) ─────────────
# Refuses any tier dir without the .backup-complete marker, so a failed
# capture can never be pushed even when invoked standalone.
if ! "${SCRIPT_DIR}/backup-offsite.sh" "$TIER"; then
    echo "$(date -Iseconds) WARN: ${TIER} offsite push failed (local backup is intact)" >&2
    exit 2
fi
