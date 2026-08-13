#!/usr/bin/env bash
# Restore the latest registry snapshot to scratch space and verify it. Never writes live state.

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/autonomy-registry}
CREDENTIALS_DIR=${CREDENTIALS_DIRECTORY:?systemd credentials are required}
PYTHON=${PYTHON:-$APP_DIR/venv/bin/python}
RESTIC=${RESTIC:-/usr/bin/restic}
TARGET=${1:?usage: restore-registry.sh <empty-target-directory> [snapshot-id]}
SNAPSHOT=${2:-latest}

case "$TARGET" in
/tmp/* | /var/tmp/*) ;;
*)
    echo "registry restore: scratch target must be under /tmp or /var/tmp" >&2
    exit 1
    ;;
esac

if [[ -e "$TARGET" ]] && [[ -n "$(find "$TARGET" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "registry restore: target must be empty: $TARGET" >&2
    exit 1
fi
mkdir -p "$TARGET"

export RESTIC_REPOSITORY="$(<"$CREDENTIALS_DIR/repository")"
export RESTIC_PASSWORD_FILE="$CREDENTIALS_DIR/restic-password"
export AWS_ACCESS_KEY_ID="$(<"$CREDENTIALS_DIR/access-key-id")"
export AWS_SECRET_ACCESS_KEY="$(<"$CREDENTIALS_DIR/secret-access-key")"

"$RESTIC" restore "$SNAPSHOT" \
    --host auto-network-registry \
    --tag service=auto-network-registry \
    --target "$TARGET"

DATABASE=$(find "$TARGET" -type f -name registry.db -print -quit)
MANIFEST=$(find "$TARGET" -type f -name manifest.json -print -quit)
if [[ -z "$DATABASE" || -z "$MANIFEST" ]]; then
    echo "registry restore: snapshot lacks registry.db or manifest.json" >&2
    exit 1
fi
"$PYTHON" "$APP_DIR/tools/network/registry/deploy/registry_snapshot.py" verify \
    --database "$DATABASE" --metadata "$MANIFEST"
echo "registry restore: verified in $TARGET"
