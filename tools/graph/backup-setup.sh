#!/usr/bin/env bash
# Offsite backup setup -- credentials go into the VAULT, never a file
# (auto-5gdao; operator ruling: the vault is the only source).
#
# Usage:
#   tools/graph/backup-setup.sh
#
# Takes no secret on its command line (argv is visible to every process).
# It:
#   1. Installs rclone + restic if missing (sudo apt)
#   2. Prints the three vault seals to run -- each prompts for its value:
#        backup.restic-password, backup.b2-key-id, backup.b2-application-key
#   3. Says where provider and bucket are set (the dashboard's /backup page)
#   4. Prints the crontab state for backup-all.sh
#
# The dashboard releases the sealed values into the host's ramfs key cache
# (/run/autonomy-keycache/backup/) whenever the vault is unlocked, and the
# hourly/daily cron runs read them from there (backup-env.sh). Only b2 has
# vault rows defined (tools/dashboard/plugins/backup/credentials.py).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "==> Installing rclone + restic (if missing)..."
MISSING=()
for bin in rclone restic; do
    command -v "$bin" >/dev/null 2>&1 || MISSING+=("$bin")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    sudo apt-get update
    sudo apt-get install -y "${MISSING[@]}"
fi

cat <<GUIDE

==> Seal the offsite credentials into the vault (audited tier; each prompts,
    nothing on argv). For an EXISTING repository, the restic password must be
    the one it was created with -- a different one cannot read it.

      graph vault seal backup.restic-password    --tier audited --prompt
      graph vault seal backup.b2-key-id          --tier audited --prompt
      graph vault seal backup.b2-application-key --tier audited --prompt

==> Set provider (b2) and bucket on the dashboard's /backup page (or PUT
    /api/backup/config {"offsite_provider": "b2", "offsite_bucket": "..."}).
    Saving it releases the credentials for the host cron run.

GUIDE

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
