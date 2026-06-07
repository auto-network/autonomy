#!/usr/bin/env bash
# Push local backups + loose-file state to an offsite restic repo.
# One-file config: agents/backup.env.  Everything else is auto.
#
# Usage:   tools/graph/backup-offsite.sh [hourly|daily]
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
PW_FILE="${REPO_ROOT}/agents/.restic.pw"

TIER="${1:-hourly}"
case "$TIER" in
    hourly|daily) ;;
    *) echo "Usage: $0 {hourly|daily}" >&2; exit 1 ;;
esac

if [[ ! -f "$ENV_FILE" ]]; then
    echo "offsite: no $ENV_FILE — skipping (run tools/graph/backup-setup.sh to enable)"
    exit 0
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

LATEST_TIER_DIR="$(ls -1dt "${REPO_ROOT}/data/backups/${TIER}"/*/ 2>/dev/null | head -1)"
if [[ -z "${LATEST_TIER_DIR}" ]]; then
    echo "offsite: no local ${TIER} backup — run backup-all.sh ${TIER} first" >&2
    exit 1
fi

echo "offsite: snapshot start ${STAMP} tier=${TIER} provider=${BACKUP_PROVIDER}"

# ── DB dumps ──────────────────────────────────────────────────────────
restic backup \
    --tag "tier=${TIER}" --tag "kind=db" \
    --host "${HOST}" \
    "${LATEST_TIER_DIR%/}"

# ── Loose-file repo data ──────────────────────────────────────────────
restic backup \
    --tag "tier=${TIER}" --tag "kind=data" \
    --host "${HOST}" \
    --exclude "*.log" --exclude "*.pid" --exclude "*-wal" --exclude "*-shm" \
    --exclude "data/backups" \
    "${REPO_ROOT}/data/agent-runs" \
    "${REPO_ROOT}/data/attachments" \
    "${REPO_ROOT}/data/uploads" \
    "${REPO_ROOT}/data/experiments" \
    "${REPO_ROOT}/data/chatgpt" \
    "${REPO_ROOT}/data/claude" \
    "${REPO_ROOT}/data/tls.crt" \
    "${REPO_ROOT}/data/tls.key" 2>/dev/null || true

# ── Host Claude Code state ────────────────────────────────────────────
restic backup \
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
        --tag "tier=${TIER}" --tag "kind=git" \
        --host "${HOST}" \
        --stdin --stdin-filename "autonomy.bundle" < "$BUNDLE_TMP"
fi
rm -f "$BUNDLE_TMP"

# ── Crontab ───────────────────────────────────────────────────────────
crontab -l 2>/dev/null | restic backup \
    --tag "tier=${TIER}" --tag "kind=crontab" \
    --host "${HOST}" \
    --stdin --stdin-filename "crontab.txt" || true

# ── Retention ─────────────────────────────────────────────────────────
# Group by tags, NOT the default host,paths. The db snapshot's path carries a
# per-run timestamp (data/backups/<tier>/<stamp>), so default grouping puts
# every db snapshot in its own group of one and the keep-policy never thins
# them. Grouping by host+tags (tier=,kind=) lets all db snapshots share a
# group so retention actually applies. See graph backup-retention pitfall.
restic forget --prune \
    --group-by host,tags \
    --keep-hourly  "${RESTIC_KEEP_HOURLY:-24}" \
    --keep-daily   "${RESTIC_KEEP_DAILY:-30}" \
    --keep-weekly  "${RESTIC_KEEP_WEEKLY:-12}" \
    --keep-monthly "${RESTIC_KEEP_MONTHLY:-12}" \
    --host "${HOST}"

echo "offsite: snapshot complete ${STAMP} tier=${TIER}"
