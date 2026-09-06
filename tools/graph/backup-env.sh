#!/usr/bin/env bash
# Shared: resolve offsite credentials and derive rclone/restic env vars.
# Usage (from another script):
#   source "$(dirname "$0")/backup-env.sh"
# After sourcing, these are set and exported:
#   RESTIC_REPOSITORY, and RESTIC_PASSWORD or RESTIC_PASSWORD_FILE
#   RCLONE_CONFIG_<REMOTE>_* for the chosen provider
#
# Credential sources, in order (auto-uy896 — operator ruling: secrets
# live in the vault):
#   1. The ENVIRONMENT — BACKUP_PROVIDER/BACKUP_BUCKET plus the
#      provider's secret variables, injected by the dashboard's backup
#      plugin from autonomy.vault.audited while the vault is warm.
#   2. agents/backup.env — DEPRECATED plaintext fallback for the host
#      cron until in-node capture retires it; warns on every use.
#
# The restic repository password follows the same rule: RESTIC_PASSWORD
# from the environment (vault row backup.restic-password) wins;
# agents/.restic.pw is the legacy file fallback.

SCRIPT_DIR_BE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_BE="$(cd "${SCRIPT_DIR_BE}/../.." && pwd)"
ENV_FILE_BE="${REPO_ROOT_BE}/agents/backup.env"
PW_FILE_BE="${REPO_ROOT_BE}/agents/.restic.pw"

if [[ -n "${BACKUP_PROVIDER:-}" && -n "${BACKUP_BUCKET:-}" ]]; then
    : # vault-released environment credentials — the intended path
elif [[ -f "$ENV_FILE_BE" ]]; then
    echo "backup-env: WARNING — agents/backup.env is deprecated plaintext;" \
         "seal the credentials into the vault (audited tier, auto-uy896)" >&2
    # shellcheck disable=SC1090
    source "$ENV_FILE_BE"
else
    echo "backup-env: no offsite credentials — neither the environment" \
         "(vault-released) nor $ENV_FILE_BE provides them" >&2
    return 1 2>/dev/null || exit 1
fi
: "${BACKUP_PROVIDER:?offsite provider unset}"
: "${BACKUP_BUCKET:?offsite bucket unset}"

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

if [[ -n "${RESTIC_PASSWORD:-}" ]]; then
    export RESTIC_PASSWORD
elif [[ -f "$PW_FILE_BE" ]]; then
    export RESTIC_PASSWORD_FILE="$PW_FILE_BE"
else
    umask 077
    openssl rand -base64 32 > "$PW_FILE_BE"
    export RESTIC_PASSWORD_FILE="$PW_FILE_BE"
fi
export RESTIC_REPOSITORY="rclone:${REMOTE}:${BACKUP_BUCKET}/restic"
