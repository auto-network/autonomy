#!/usr/bin/env bash
# Install the auto.network apex vhost on registry-ash-1's Caddy, safely.
#
# Adds ONLY the auto.network → 127.0.0.1:8477 reverse proxy (caddy/auto.network.caddy),
# leaving every other vhost (registry.auto.network, relay.auto.network, …) and
# the registry --base-url untouched. The change is:
#
#   1. capture the live /etc/caddy/Caddyfile back into this repo BEFORE editing
#      (it is currently "host-only" — uncaptured — so the VM is not yet
#      clean-room reproducible; this is where that stops being true), and keep
#      a timestamped on-host backup for immediate rollback;
#   2. append the apex vhost between BEGIN/END markers — idempotent, no edits
#      to any existing line (guards against Caddy collateral edits);
#   3. `caddy validate` the result; on ANY validation failure, restore the
#      backup and abort WITHOUT reloading;
#   4. reload Caddy only after validation passes;
#   5. capture the post-change Caddyfile back into the repo too.
#
# PRECONDITION — ordering: the apex A record must already resolve to this box
# (run add-apex-a-record.sh on auto-ash-1 and let it propagate) so Caddy can
# mint the auto.network certificate over HTTP-01. This script refuses to run
# until auto.network resolves to the expected IP from multiple resolvers.
#
# Usage:
#   ./deploy-apex-vhost.sh [ssh-target]     # default: root@5.161.219.195
#
# This is the estate's ONE apex Caddy change. It deliberately does not manage
# the rest of Caddy — that is the estate golden-snapshot's job (auto-pqcsh).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

TARGET="${1:-root@5.161.219.195}"
APEX=auto.network
APEX_IP=5.161.219.195
REMOTE_CADDYFILE=/etc/caddy/Caddyfile
FRAGMENT=caddy/auto.network.caddy
CAPTURE=caddy/registry-ash-1.Caddyfile.captured
MARK_BEGIN="# >>> auto-network-estate: auto.network apex (bead auto-9q7a5) >>>"
MARK_END="# <<< auto-network-estate: auto.network apex <<<"

# Never let this land on the legacy pet (live mail); the apex belongs on the
# registry box, never on auto-ash-1.
case "$TARGET" in
*5.161.179.179* | *auto-ash-1* | *mail.auto.network*)
    echo "refusing: the apex vhost belongs on registry-ash-1, not the pet" >&2
    exit 1
    ;;
esac

# Ordering gate: cert issuance needs DNS first. Verify apex resolves to the box
# from multiple public resolvers before we ask Caddy to obtain a certificate.
echo "==> checking $APEX resolves to $APEX_IP (cert issuance needs DNS first)"
resolved_ok=0
for r in 1.1.1.1 8.8.8.8 9.9.9.9; do
    got=$(dig +short @"$r" "$APEX" A 2>/dev/null | tail -n1 || true)
    echo "    @$r → ${got:-<none>}"
    [ "$got" = "$APEX_IP" ] && resolved_ok=$((resolved_ok + 1))
done
if [ "$resolved_ok" -lt 2 ]; then
    echo "refusing: $APEX does not yet resolve to $APEX_IP from >=2 resolvers." >&2
    echo "Run add-apex-a-record.sh on auto-ash-1 and let DNS propagate first." >&2
    exit 1
fi

FRAGMENT_CONTENT=$(cat "$FRAGMENT")

echo "==> capturing current $REMOTE_CADDYFILE from $TARGET into $CAPTURE (pre-change)"
ssh "$TARGET" "cat $REMOTE_CADDYFILE" > "$CAPTURE.pre"

# All host-side mutation happens in one bash payload so it is atomic per-step
# and can self-rollback. Markers + fragment are passed via env to avoid quoting.
ssh "$TARGET" \
    MARK_BEGIN="$MARK_BEGIN" MARK_END="$MARK_END" FRAGMENT="$FRAGMENT_CONTENT" \
    REMOTE_CADDYFILE="$REMOTE_CADDYFILE" bash -s <<'REMOTE'
set -euo pipefail

ts=$(date +%Y%m%d-%H%M%S)
backup="${REMOTE_CADDYFILE}.bak.${ts}"
cp -a "$REMOTE_CADDYFILE" "$backup"
echo "    backup: $backup"

if grep -qF "$MARK_BEGIN" "$REMOTE_CADDYFILE"; then
    echo "    apex vhost marker already present — idempotent, no append"
else
    {
        printf '\n%s\n' "$MARK_BEGIN"
        printf '%s\n' "$FRAGMENT"
        printf '%s\n' "$MARK_END"
    } >> "$REMOTE_CADDYFILE"
    echo "    appended apex vhost block"
fi

# Validate BEFORE reload. Debian's caddy package ships the binary + a
# caddy.service; validate against the exact file we just wrote.
if ! caddy validate --config "$REMOTE_CADDYFILE" --adapter caddyfile; then
    echo "    caddy validate FAILED — restoring backup, NOT reloading" >&2
    cp -a "$backup" "$REMOTE_CADDYFILE"
    exit 1
fi
echo "    caddy validate OK"

# Reload the running server with the validated config (zero-downtime).
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy; then
    systemctl reload caddy
else
    caddy reload --config "$REMOTE_CADDYFILE" --adapter caddyfile
fi
echo "    caddy reloaded"
REMOTE

echo "==> capturing post-change $REMOTE_CADDYFILE into $CAPTURE"
ssh "$TARGET" "cat $REMOTE_CADDYFILE" > "$CAPTURE"
rm -f "$CAPTURE.pre"

echo "==> done. Caddy will obtain the $APEX certificate on first HTTPS hit."
echo "    Verify end-to-end with:  ./verify-apex.sh"
echo "    Rollback (on host):      cp <printed backup> $REMOTE_CADDYFILE && systemctl reload caddy"
