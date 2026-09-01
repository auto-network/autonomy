#!/usr/bin/env bash
# Install the estate's complete, repository-owned Caddy configuration on a
# box, plus the one per-host value it needs: the bind IP.
#
# Usage:
#   deploy-caddy.sh [ssh-target] [--bind-ip <ip>]
#     ssh-target  default root@5.161.219.195
#     --bind-ip   default: the target's own IP (the host part of ssh-target)
#
# ONE authored file (caddy/estate.Caddyfile) is deployed byte-identical to
# every box; the bind IP is supplied to caddy as $AUTONOMY_BIND_IP through a
# systemd drop-in, so the config file is truly host-independent. The
# candidate is validated on the target (with the env set) before it can
# replace the live file. Installation is atomic, keeps a timestamped
# rollback copy, and reloads Caddy without restarting the registry. No
# live-file capture occurs.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

TARGET=root@5.161.219.195
BIND_IP=""
while [ $# -gt 0 ]; do
    case $1 in
    --bind-ip) BIND_IP=$2; shift 2 ;;
    -*) echo "unknown arg: $1" >&2; exit 1 ;;
    *) TARGET=$1; shift ;;
    esac
done
# Default the bind IP to the target's own address (the herd norm: a box
# binds itself). Override for an anycast address.
[ -n "$BIND_IP" ] || BIND_IP=${TARGET#*@}

SOURCE=caddy/estate.Caddyfile
DEST=/etc/caddy/Caddyfile

case "$TARGET" in
*5.161.179.179* | *auto-ash-1* | *mail.auto.network*)
    echo "refusing: estate Caddy does not belong on the legacy pet" >&2
    exit 1
    ;;
esac

[ -f "$SOURCE" ] || { echo "missing authored Caddyfile: $SOURCE" >&2; exit 1; }
EXPECTED_SHA=$(sha256sum "$SOURCE" | awk '{print $1}')
REMOTE_TMP=$(ssh "$TARGET" "mktemp /tmp/autonomy-registry.Caddyfile.XXXXXX")
cleanup() { ssh "$TARGET" "rm -f '$REMOTE_TMP'" >/dev/null 2>&1 || true; }
trap cleanup EXIT

scp -q "$SOURCE" "$TARGET:$REMOTE_TMP"
ssh "$TARGET" REMOTE_TMP="$REMOTE_TMP" DEST="$DEST" EXPECTED_SHA="$EXPECTED_SHA" \
    BIND_IP="$BIND_IP" bash -s <<'REMOTE'
set -euo pipefail

actual_sha=$(sha256sum "$REMOTE_TMP" | awk '{print $1}')
[ "$actual_sha" = "$EXPECTED_SHA" ] || {
    echo "refusing: transferred Caddyfile checksum mismatch" >&2
    exit 1
}

# The one per-host value: caddy resolves {$AUTONOMY_BIND_IP} from its unit
# environment at config-load time, so runtime AND reload need it set. A
# systemd drop-in carries it; this is the only place a box's IP is written.
install -d /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/autonomy-bind.conf <<CONF
[Service]
Environment=AUTONOMY_BIND_IP=${BIND_IP}
CONF
systemctl daemon-reload

# Validation is load-bearing and happens before any write to DEST, with the
# same env caddy will load, so an unresolved placeholder fails here.
AUTONOMY_BIND_IP="$BIND_IP" caddy validate --config "$REMOTE_TMP" --adapter caddyfile

if [ -f "$DEST" ] && cmp -s "$REMOTE_TMP" "$DEST"; then
    echo "Caddyfile already current ($EXPECTED_SHA); reloading for bind env"
    systemctl reload caddy
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

echo "installed Caddyfile $EXPECTED_SHA (bind $BIND_IP); rollback: ${backup:-none}"
REMOTE
