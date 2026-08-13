#!/usr/bin/env bash
# Create one WAL-correct SQLite snapshot and commit it to the dedicated restic repo.

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/autonomy-registry}
DB_PATH=${REGISTRY_DB_PATH:-/var/lib/autonomy-registry/registry.db}
STATUS_DIR=${REGISTRY_BACKUP_STATUS_DIR:-/var/lib/autonomy-registry-backup}
CREDENTIALS_DIR=${CREDENTIALS_DIRECTORY:?systemd credentials are required}
PYTHON=${PYTHON:-$APP_DIR/venv/bin/python}
RESTIC=${RESTIC:-/usr/bin/restic}

for name in repository restic-password access-key-id secret-access-key; do
    if [[ ! -s "$CREDENTIALS_DIR/$name" ]]; then
        echo "registry backup: missing credential $name" >&2
        exit 1
    fi
done

STAGE=$(mktemp -d /tmp/autonomy-registry-backup.XXXXXX)
trap 'rm -rf -- "$STAGE"' EXIT

"$PYTHON" "$APP_DIR/tools/network/registry/deploy/registry_snapshot.py" create \
    --source "$DB_PATH" \
    --destination "$STAGE/registry.db" \
    --metadata "$STAGE/manifest.json" >/dev/null

# Only restic receives object-store credentials. The SQLite snapshot process
# runs without them and therefore cannot expose them in an unrelated traceback.
export RESTIC_REPOSITORY="$(<"$CREDENTIALS_DIR/repository")"
export RESTIC_PASSWORD_FILE="$CREDENTIALS_DIR/restic-password"
export AWS_ACCESS_KEY_ID="$(<"$CREDENTIALS_DIR/access-key-id")"
export AWS_SECRET_ACCESS_KEY="$(<"$CREDENTIALS_DIR/secret-access-key")"

OUTPUT="$STAGE/restic.json"
"$RESTIC" backup --json \
    --host auto-network-registry \
    --tag service=auto-network-registry \
    --tag kind=sqlite \
    "$STAGE/registry.db" "$STAGE/manifest.json" >"$OUTPUT"

SNAPSHOT_ID=$(
    "$PYTHON" - "$OUTPUT" <<'PY'
import json
import sys

snapshot_id = None
with open(sys.argv[1], encoding="utf-8") as source:
    for line in source:
        event = json.loads(line)
        if event.get("message_type") == "summary":
            snapshot_id = event.get("snapshot_id")
if not snapshot_id:
    raise SystemExit("restic completed without a snapshot id")
print(snapshot_id)
PY
)

install -d -m 0750 "$STATUS_DIR"
printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SNAPSHOT_ID" \
    >"$STATUS_DIR/last-success.tmp"
chmod 0640 "$STATUS_DIR/last-success.tmp"
mv "$STATUS_DIR/last-success.tmp" "$STATUS_DIR/last-success"
echo "registry backup: complete snapshot=$SNAPSHOT_ID"
