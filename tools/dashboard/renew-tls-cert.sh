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
LOG="$REPO_ROOT/data/cert-renew.log"
CERT="${AUTONOMY_TLS_CERT:-$REPO_ROOT/data/tls.crt}"

exec >>"$LOG" 2>&1

# The Tailnet name in an existing certificate's subjectAltName, if any (the
# same rule the dashboard's remote-access seed uses).
tailnet_name_from_cert() {  # tailnet_name_from_cert <cert path>
    [[ -f "$1" ]] || return 1
    openssl x509 -in "$1" -noout -ext subjectAltName 2>/dev/null \
        | tr ',' '\n' | sed -n 's/.*DNS:\([A-Za-z0-9.-]*\.ts\.net\).*/\1/p' | head -1 | tr 'A-Z' 'a-z'
}

# No machine name lives in this script. DASHBOARD_DOMAIN names the Tailnet host
# to issue for; without it the existing certificate's own name is renewed. A
# missing name is logged (the log is already open) and the run stops.
DOMAIN="${DASHBOARD_DOMAIN:-}"
if [[ -z "$DOMAIN" ]]; then
    DOMAIN="$(tailnet_name_from_cert "$CERT" || true)"
fi
if [[ -z "$DOMAIN" ]]; then
    echo "!!! $(date -Is) no DASHBOARD_DOMAIN and no .ts.net name in $CERT; nothing renewed" >&2
    exit 2
fi
echo "=== $(date -Is) renewing TLS cert for $DOMAIN ==="

if /usr/bin/tailscale cert \
      --cert-file "${AUTONOMY_TLS_CERT:-$REPO_ROOT/data/tls.crt}" \
      --key-file  "${AUTONOMY_TLS_KEY:-$REPO_ROOT/data/tls.key}" \
      "$DOMAIN"; then
    echo "cert written; restarting dashboard to load it"
    "$REPO_ROOT/tools/dashboard/start-dashboard.sh" --restart
    echo "=== $(date -Is) renewal OK ==="
else
    rc=$?
    echo "!!! $(date -Is) tailscale cert FAILED (rc=$rc) — existing cert left in place" >&2
    exit "$rc"
fi
