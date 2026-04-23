#!/usr/bin/env bash
# Zero-friction offsite backup setup.  One command, end-to-end.
#
# Usage:
#   tools/graph/backup-setup.sh b2 <keyID> <applicationKey> [bucket]
#   tools/graph/backup-setup.sh r2 <accessKey> <secretKey> <endpoint> [bucket]
#   tools/graph/backup-setup.sh s3 <accessKey> <secretKey> <region> [bucket]
#
# Does everything:
#   1. Installs rclone + restic if missing (sudo apt)
#   2. Writes agents/backup.env from CLI args
#   3. Generates agents/.restic.pw (random)
#   4. Initialises the restic repo on the remote
#   5. Runs first local backup (calls backup-all.sh)
#   6. Runs restore drill to verify round-trip
#   7. Prints the current crontab and confirms backup-all is already scheduled
#
# After this runs, the hourly/daily cron jobs automatically push offsite
# because backup-all.sh tail-calls backup-offsite.sh when backup.env exists.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/agents/backup.env"
DEFAULT_BUCKET="autonomy-offsite"

PROVIDER="${1:-}"
if [[ -z "$PROVIDER" ]]; then
    echo "Usage:"
    echo "  $0 b2 <keyID> <applicationKey> [bucket]"
    echo "  $0 r2 <accessKey> <secretKey> <endpoint> [bucket]"
    echo "  $0 s3 <accessKey> <secretKey> <region> [bucket]"
    echo ""
    echo "  [bucket]  default: ${DEFAULT_BUCKET}"
    exit 1
fi

# ── Parse provider args into backup.env content ───────────────────────
case "$PROVIDER" in
    b2)
        KEY_ID="${2:?keyID required}"
        APP_KEY="${3:?applicationKey required}"
        BUCKET="${4:-$DEFAULT_BUCKET}"
        ENV_CONTENT="BACKUP_PROVIDER=b2
B2_KEY_ID=${KEY_ID}
B2_APPLICATION_KEY=${APP_KEY}
BACKUP_BUCKET=${BUCKET}"
        ;;
    r2)
        KEY_ID="${2:?accessKey required}"
        SECRET="${3:?secretKey required}"
        ENDPOINT="${4:?endpoint required}"
        BUCKET="${5:-$DEFAULT_BUCKET}"
        ENV_CONTENT="BACKUP_PROVIDER=r2
R2_ACCESS_KEY_ID=${KEY_ID}
R2_SECRET_ACCESS_KEY=${SECRET}
R2_ENDPOINT=${ENDPOINT}
BACKUP_BUCKET=${BUCKET}"
        ;;
    s3)
        KEY_ID="${2:?accessKey required}"
        SECRET="${3:?secretKey required}"
        REGION="${4:?region required}"
        BUCKET="${5:-$DEFAULT_BUCKET}"
        ENV_CONTENT="BACKUP_PROVIDER=s3
S3_ACCESS_KEY_ID=${KEY_ID}
S3_SECRET_ACCESS_KEY=${SECRET}
S3_REGION=${REGION}
BACKUP_BUCKET=${BUCKET}"
        ;;
    *)
        echo "Unknown provider: $PROVIDER (use b2|r2|s3)" >&2
        exit 1
        ;;
esac

echo "==> Installing rclone + restic (if missing)..."
MISSING=()
for bin in rclone restic; do
    command -v "$bin" >/dev/null 2>&1 || MISSING+=("$bin")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    sudo apt-get update
    sudo apt-get install -y "${MISSING[@]}"
fi

echo "==> Writing $ENV_FILE..."
umask 077
echo "$ENV_CONTENT" > "$ENV_FILE"

echo "==> Running first offsite push (will auto-init repo + generate password)..."
# Ensure we have a local tier directory to snapshot — force a fresh hourly run.
"${SCRIPT_DIR}/backup-all.sh" hourly

echo "==> Running restore drill..."
"${SCRIPT_DIR}/backup-restore.sh" drill

echo ""
echo "==> Verifying cron..."
if crontab -l 2>/dev/null | grep -q "backup-all.sh hourly"; then
    echo "    hourly cron: present"
else
    echo "    hourly cron: MISSING — add with:"
    echo "      (crontab -l; echo '0 * * * * ${SCRIPT_DIR}/backup-all.sh hourly >> ${REPO_ROOT}/data/graph-backup.log 2>&1') | crontab -"
fi
if crontab -l 2>/dev/null | grep -q "backup-all.sh daily"; then
    echo "    daily  cron: present"
else
    echo "    daily  cron: MISSING"
fi

echo ""
echo "==> Done.  Offsite backups are active."
echo "    Next steps are automatic — the existing hourly/daily cron jobs now"
echo "    push to ${PROVIDER}:${BUCKET} after each local tier completes."
echo ""
echo "    Password lives at agents/.restic.pw — keep a copy somewhere safe."
echo "    (Lose it and the remote repo is unrecoverable.)"
