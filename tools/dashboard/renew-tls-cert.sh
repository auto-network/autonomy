#!/usr/bin/env bash
# Renew the dashboard's Tailscale Let's Encrypt TLS cert and reload the dashboard.
#
# tailscale cert issues a 90-day cert with NO auto-renewal, so a cron job runs
# this monthly (huge margin vs 90 days). Reload is non-privileged: tailscale
# serve owns :443 (TCP passthrough), the dashboard only binds :8080, so no sudo.
#
# Installed crontab entry (monthly, 03:00 on the 1st):
#   0 3 1 * * "$AUTONOMY_ROOT/tools/dashboard/renew-tls-cert.sh"
# (AUTONOMY_ROOT defaults to the repo this script lives in; DASHBOARD_DOMAIN
#  is the Tailscale hostname to issue the cert for.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${AUTONOMY_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
DOMAIN="${DASHBOARD_DOMAIN:-desktop-noft5ms.tail35c24e.ts.net}"
LOG="$REPO_ROOT/data/cert-renew.log"

exec >>"$LOG" 2>&1
echo "=== $(date -Is) renewing TLS cert for $DOMAIN ==="

if /usr/bin/tailscale cert \
      --cert-file "$REPO_ROOT/data/tls.crt" \
      --key-file  "$REPO_ROOT/data/tls.key" \
      "$DOMAIN"; then
    echo "cert written; restarting dashboard to load it"
    "$REPO_ROOT/tools/dashboard/start-dashboard.sh" --restart
    echo "=== $(date -Is) renewal OK ==="
else
    rc=$?
    echo "!!! $(date -Is) tailscale cert FAILED (rc=$rc) — existing cert left in place" >&2
    exit "$rc"
fi
