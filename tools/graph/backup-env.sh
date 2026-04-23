#!/usr/bin/env bash
# Shared: source backup.env and derive rclone/restic env vars from provider.
# Usage (from another script):
#   source "$(dirname "$0")/backup-env.sh"
# After sourcing, these are set and exported:
#   RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE
#   RCLONE_CONFIG_<REMOTE>_* for the chosen provider

SCRIPT_DIR_BE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_BE="$(cd "${SCRIPT_DIR_BE}/../.." && pwd)"
ENV_FILE_BE="${REPO_ROOT_BE}/agents/backup.env"
PW_FILE_BE="${REPO_ROOT_BE}/agents/.restic.pw"

if [[ ! -f "$ENV_FILE_BE" ]]; then
    echo "backup-env: no $ENV_FILE_BE — run tools/graph/backup-setup.sh first" >&2
    return 1 2>/dev/null || exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE_BE"
: "${BACKUP_PROVIDER:?set in $ENV_FILE_BE}"
: "${BACKUP_BUCKET:?set in $ENV_FILE_BE}"

REMOTE="$BACKUP_PROVIDER"
case "$BACKUP_PROVIDER" in
    b2)
        : "${B2_KEY_ID:?}" "${B2_APPLICATION_KEY:?}"
        export "RCLONE_CONFIG_${REMOTE^^}_TYPE=b2"
        export "RCLONE_CONFIG_${REMOTE^^}_ACCOUNT=$B2_KEY_ID"
        export "RCLONE_CONFIG_${REMOTE^^}_KEY=$B2_APPLICATION_KEY"
        export "RCLONE_CONFIG_${REMOTE^^}_HARD_DELETE=true"
        ;;
    r2)
        : "${R2_ACCESS_KEY_ID:?}" "${R2_SECRET_ACCESS_KEY:?}" "${R2_ENDPOINT:?}"
        export "RCLONE_CONFIG_${REMOTE^^}_TYPE=s3"
        export "RCLONE_CONFIG_${REMOTE^^}_PROVIDER=Cloudflare"
        export "RCLONE_CONFIG_${REMOTE^^}_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID"
        export "RCLONE_CONFIG_${REMOTE^^}_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY"
        export "RCLONE_CONFIG_${REMOTE^^}_ENDPOINT=$R2_ENDPOINT"
        export "RCLONE_CONFIG_${REMOTE^^}_REGION=auto"
        ;;
    s3)
        : "${S3_ACCESS_KEY_ID:?}" "${S3_SECRET_ACCESS_KEY:?}" "${S3_REGION:?}"
        export "RCLONE_CONFIG_${REMOTE^^}_TYPE=s3"
        export "RCLONE_CONFIG_${REMOTE^^}_PROVIDER=AWS"
        export "RCLONE_CONFIG_${REMOTE^^}_ACCESS_KEY_ID=$S3_ACCESS_KEY_ID"
        export "RCLONE_CONFIG_${REMOTE^^}_SECRET_ACCESS_KEY=$S3_SECRET_ACCESS_KEY"
        export "RCLONE_CONFIG_${REMOTE^^}_REGION=$S3_REGION"
        ;;
    *)
        echo "backup-env: unknown BACKUP_PROVIDER=$BACKUP_PROVIDER" >&2
        return 1 2>/dev/null || exit 1
        ;;
esac

if [[ ! -f "$PW_FILE_BE" ]]; then
    umask 077
    openssl rand -base64 32 > "$PW_FILE_BE"
fi
export RESTIC_PASSWORD_FILE="$PW_FILE_BE"
export RESTIC_REPOSITORY="rclone:${REMOTE}:${BACKUP_BUCKET}/restic"
