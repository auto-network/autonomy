#!/usr/bin/env bash
# Install registry-ash-1's complete, repository-owned Caddy configuration.
#
# The candidate is validated on the target before it can replace the live
# file. Installation is atomic, keeps a timestamped rollback copy, and reloads
# Caddy without restarting the registry process. No live-file capture occurs:
# caddy/registry-ash-1.Caddyfile is the source of truth.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

TARGET=${1:-root@5.161.219.195}
SOURCE=caddy/registry-ash-1.Caddyfile
DEST=/etc/caddy/Caddyfile

case "$TARGET" in
*5.161.179.179* | *auto-ash-1* | *mail.auto.network*)
    echo "refusing: registry Caddy belongs on registry-ash-1, not the pet" >&2
    exit 1
    ;;
esac

[ -f "$SOURCE" ] || { echo "missing authored Caddyfile: $SOURCE" >&2; exit 1; }
EXPECTED_SHA=$(sha256sum "$SOURCE" | awk '{print $1}')
REMOTE_TMP=$(ssh "$TARGET" "mktemp /tmp/autonomy-registry.Caddyfile.XXXXXX")
cleanup() { ssh "$TARGET" "rm -f '$REMOTE_TMP'" >/dev/null 2>&1 || true; }
trap cleanup EXIT

scp -q "$SOURCE" "$TARGET:$REMOTE_TMP"
ssh "$TARGET" REMOTE_TMP="$REMOTE_TMP" DEST="$DEST" EXPECTED_SHA="$EXPECTED_SHA" bash -s <<'REMOTE'
set -euo pipefail

actual_sha=$(sha256sum "$REMOTE_TMP" | awk '{print $1}')
[ "$actual_sha" = "$EXPECTED_SHA" ] || {
    echo "refusing: transferred Caddyfile checksum mismatch" >&2
    exit 1
}

# Validation is load-bearing and must happen before any write to DEST.
caddy validate --config "$REMOTE_TMP" --adapter caddyfile

if [ -f "$DEST" ] && cmp -s "$REMOTE_TMP" "$DEST"; then
    echo "Caddyfile already current ($EXPECTED_SHA); no reload"
    exit 0
fi

if [ -f "$DEST" ]; then
    echo "Live Caddyfile differs from the authored file; replacing this diff:"
    diff -u "$DEST" "$REMOTE_TMP" || true
else
    echo "No live Caddyfile exists; installing the complete authored file"
fi

ts=$(date -u +%Y%m%d-%H%M%S)
backup="${DEST}.bak.${ts}"
[ ! -e "$DEST" ] || cp -a "$DEST" "$backup"
install -m 0644 -o root -g root "$REMOTE_TMP" "${DEST}.new"
mv "${DEST}.new" "$DEST"

if ! systemctl reload caddy; then
    echo "reload failed; restoring $backup" >&2
    if [ -e "$backup" ]; then
        cp -a "$backup" "$DEST"
    else
        rm -f "$DEST"
    fi
    systemctl reload caddy || true
    exit 1
fi

echo "installed Caddyfile $EXPECTED_SHA; rollback: $backup"
REMOTE
