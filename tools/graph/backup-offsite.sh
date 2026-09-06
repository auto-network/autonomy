#!/usr/bin/env bash
# Push local backups + loose-file state to an offsite restic repo.
# One-file config: agents/backup.env.  Everything else is auto.
#
# Usage:   tools/graph/backup-offsite.sh [hourly|daily]
#
# Data-root contract (auto-iwct5): loose-file paths come from the store
# manifest via backup_stores.py (AUTONOMY_DATA_ROOT-aware), never from this
# checkout's own data/. The kind=db snapshot pushes the newest local tier
# dir and REFUSES one without backup-all.sh's .backup-complete marker — a
# failed or partial capture is never shipped offsite.
#
# Auto-behavior on first run:
#   - installs rclone + restic via apt (prompts for sudo)
#   - generates agents/.restic.pw (random 32 bytes, 600 perms)
#   - runs restic init on the remote repo
#
# Provider is selected by BACKUP_PROVIDER in backup.env.  rclone config is
# injected via RCLONE_CONFIG_<REMOTE>_* env vars — no config file needed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/agents/backup.env"
PYTHON="${REPO_ROOT}/.venv/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

DATA_ROOT="${AUTONOMY_DATA_ROOT:-${REPO_ROOT}/data}"
BACKUP_ROOT="${AUTONOMY_BACKUP_ROOT:-${DATA_ROOT}/backups}"

TIER="${1:-hourly}"
case "$TIER" in
    hourly|daily) ;;
    *) echo "Usage: $0 {hourly|daily}" >&2; exit 1 ;;
esac

# Configured = vault-released environment credentials (the intended
# path, auto-uy896) OR the deprecated agents/backup.env fallback.
# backup-all.sh greps this "skipping" line to stamp offsite=skipped.
if [[ -z "${BACKUP_PROVIDER:-}" || -z "${BACKUP_BUCKET:-}" ]] && [[ ! -f "$ENV_FILE" ]]; then
    echo "offsite: not configured (no vault-released environment, no $ENV_FILE) — skipping"
    exit 0
fi

# ── The local capture to ship: newest tier dir, marker-gated ──────────
LATEST_TIER_DIR="$(ls -1dt "${BACKUP_ROOT}/${TIER}"/*/ 2>/dev/null | head -1 || true)"
if [[ -z "${LATEST_TIER_DIR}" ]]; then
    echo "offsite: no local ${TIER} backup under ${BACKUP_ROOT} — run backup-all.sh ${TIER} first" >&2
    exit 1
fi
if [[ ! -f "${LATEST_TIER_DIR%/}/.backup-complete" ]]; then
    echo "offsite: ${LATEST_TIER_DIR%/} has no .backup-complete marker" \
         "(failed, partial, or pre-contract capture) — refusing to push it" >&2
    exit 1
fi

# Install deps if missing (one-time)
for bin in rclone restic; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "offsite: installing $bin via apt..."
        sudo apt-get install -y "$bin"
    fi
done

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/backup-env.sh"

# ── Init repo on first run ────────────────────────────────────────────
if ! restic snapshots --no-lock --quiet >/dev/null 2>&1; then
    echo "offsite: initialising restic repo at $RESTIC_REPOSITORY"
    restic init
fi

STAMP="$(date -Iseconds)"
HOST="$(hostname -s)"

echo "offsite: snapshot start ${STAMP} tier=${TIER} provider=${BACKUP_PROVIDER}"

# ── DB dumps + key material (the marker-verified tier dir) ────────────
restic backup \
    --quiet \
    --tag "tier=${TIER}" --tag "kind=db" \
    --host "${HOST}" \
    "${LATEST_TIER_DIR%/}"

# ── Loose-file data dirs (manifest verify-dirs + legacy extras) ───────
mapfile -t DATA_PATHS < <("$PYTHON" "${SCRIPT_DIR}/backup_stores.py" offsite-data)
if [[ ${#DATA_PATHS[@]} -gt 0 ]]; then
    restic backup \
        --quiet \
        --tag "tier=${TIER}" --tag "kind=data" \
        --host "${HOST}" \
        --exclude "*.log" --exclude "*.pid" --exclude "*-wal" --exclude "*-shm" \
        --exclude "${BACKUP_ROOT}" \
        "${DATA_PATHS[@]}"
else
    echo "offsite: WARN — no loose-file data dirs found under ${DATA_ROOT}" >&2
fi

# ── Host Claude Code state (host-only, best-effort) ───────────────────
restic backup \
    --quiet \
    --tag "tier=${TIER}" --tag "kind=claude" \
    --host "${HOST}" \
    --exclude "*.log" --exclude "statsig" --exclude "cache" \
    --exclude "shell-snapshots" --exclude "file-history" \
    "${HOME}/.claude.json" \
    "${HOME}/.claude/CLAUDE.md" \
    "${HOME}/.claude/settings.json" \
    "${HOME}/.claude/settings.local.json" \
    "${HOME}/.claude/skills" \
    "${HOME}/.claude/plugins" \
    "${HOME}/.claude/projects" 2>/dev/null || true

# ── Git bundle (full history, offline-restorable) ─────────────────────
# `git bundle --all` packs every branch/tag/ref into one file.  Restic dedups
# the pack so consecutive bundles cost near-zero.  Restore: `git clone bundle`.
BUNDLE_TMP="$(mktemp -t autonomy-XXXXXX.bundle)"
if git -C "$REPO_ROOT" bundle create "$BUNDLE_TMP" --all 2>/dev/null; then
    restic backup \
        --quiet \
        --tag "tier=${TIER}" --tag "kind=git" \
        --host "${HOST}" \
        --stdin --stdin-filename "autonomy.bundle" < "$BUNDLE_TMP"
fi
rm -f "$BUNDLE_TMP"

# ── Crontab ───────────────────────────────────────────────────────────
crontab -l 2>/dev/null | restic backup \
    --quiet \
    --tag "tier=${TIER}" --tag "kind=crontab" \
    --host "${HOST}" \
    --stdin --stdin-filename "crontab.txt" || true

# ── Retention ─────────────────────────────────────────────────────────
# Group by tags, NOT the default host,paths. The db snapshot's path carries a
# per-run timestamp (<backup root>/<tier>/<stamp>), so default grouping puts
# every db snapshot in its own group of one and the keep-policy never thins
# them. Grouping by host+tags (tier=,kind=) lets all db snapshots share a
# group so retention actually applies. See graph backup-retention pitfall.
restic forget --prune \
    --quiet \
    --group-by host,tags \
    --keep-hourly  "${RESTIC_KEEP_HOURLY:-24}" \
    --keep-daily   "${RESTIC_KEEP_DAILY:-30}" \
    --keep-weekly  "${RESTIC_KEEP_WEEKLY:-12}" \
    --keep-monthly "${RESTIC_KEEP_MONTHLY:-12}" \
    --host "${HOST}"

# Cumulative repository size (the operator's "how much is on the
# provider"): raw-data mode = unique bytes stored after dedup. Emitted
# in a stable grep-able line; backup-all.sh forwards it into the
# run-report's offsite stamp.
PYTHON_BE="${REPO_ROOT}/.venv/bin/python3"
[[ -x "$PYTHON_BE" ]] || PYTHON_BE="$(command -v python3)"
REPO_BYTES="$(restic stats --json --mode raw-data 2>/dev/null \
    | "$PYTHON_BE" -c 'import json,sys; print(int(json.load(sys.stdin).get("total_size", 0)))' \
    2>/dev/null || echo 0)"
echo "offsite: repository raw size ${REPO_BYTES} bytes"

echo "offsite: snapshot complete ${STAMP} tier=${TIER}"
