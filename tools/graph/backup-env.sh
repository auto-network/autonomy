#!/usr/bin/env bash
# Shared: resolve offsite credentials and derive rclone/restic env vars.
# Usage (from another script):
#   source "$(dirname "$0")/backup-env.sh"
# After sourcing, these are set and exported:
#   RESTIC_REPOSITORY and RESTIC_PASSWORD_FILE (a released ramfs file)
#   RCLONE_CONFIG_<REMOTE>_* for the chosen provider
#
# ONE source (auto-5gdao; operator ruling: secrets live only in the vault):
# the files the dashboard RELEASES from the audited vault into the host's
# ramfs key cache, <keycache>/backup/ -- restic-password, b2-key-id,
# b2-application-key, and the non-secret offsite.env (provider, bucket).
# The dashboard can open the vault; this host cron process cannot, so it
# reads what was released. No plaintext file is read: agents/backup.env and
# agents/.restic.pw are gone, and nothing generates a password (a new random
# one would silently start a second, unreadable repository). Absent files
# mean the vault is cold (after a reboot, until unlock): callers skip.

RELEASED_BE="${AUTONOMY_KEYCACHE_MOUNT:-/run/autonomy-keycache}/backup"

if [[ ! -s "$RELEASED_BE/offsite.env" || ! -s "$RELEASED_BE/restic-password" ]]; then
    echo "backup-env: vault-cold — no offsite credentials released into" \
         "$RELEASED_BE (unlock the vault on the dashboard)" >&2
    return 1 2>/dev/null || exit 1
fi
# Read the two keys; never source the file (its values are operator-set).
BACKUP_PROVIDER="$(sed -n 's/^BACKUP_PROVIDER=//p' "$RELEASED_BE/offsite.env" | head -1)"
BACKUP_BUCKET="$(sed -n 's/^BACKUP_BUCKET=//p' "$RELEASED_BE/offsite.env" | head -1)"
export BACKUP_PROVIDER BACKUP_BUCKET
: "${BACKUP_PROVIDER:?offsite provider unset}"
: "${BACKUP_BUCKET:?offsite bucket unset}"

REMOTE="$BACKUP_PROVIDER"
case "$BACKUP_PROVIDER" in
    b2)
        B2_KEY_ID="$(cat "$RELEASED_BE/b2-key-id" 2>/dev/null || true)"
        B2_APPLICATION_KEY="$(cat "$RELEASED_BE/b2-application-key" 2>/dev/null || true)"
        : "${B2_KEY_ID:?b2 key id not released}" "${B2_APPLICATION_KEY:?b2 application key not released}"
        export "RCLONE_CONFIG_${REMOTE^^}_TYPE=b2"
        export "RCLONE_CONFIG_${REMOTE^^}_ACCOUNT=$B2_KEY_ID"
        export "RCLONE_CONFIG_${REMOTE^^}_KEY=$B2_APPLICATION_KEY"
        export "RCLONE_CONFIG_${REMOTE^^}_HARD_DELETE=true"
        ;;
    r2|s3)
        # No vault rows are defined for these providers' credentials, and
        # no plaintext source remains: refuse by name rather than guess.
        echo "backup-env: provider $BACKUP_PROVIDER has no vault-released" \
             "credentials (only b2 is defined: plugins/backup/credentials.py)" >&2
        return 1 2>/dev/null || exit 1
        ;;
    *)
        echo "backup-env: unknown BACKUP_PROVIDER=$BACKUP_PROVIDER" >&2
        return 1 2>/dev/null || exit 1
        ;;
esac

unset RESTIC_PASSWORD
export RESTIC_PASSWORD_FILE="$RELEASED_BE/restic-password"
export RESTIC_REPOSITORY="rclone:${REMOTE}:${BACKUP_BUCKET}/restic"
