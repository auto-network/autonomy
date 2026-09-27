#!/usr/bin/env bash
# Renew the dashboard's Tailscale Let's Encrypt TLS cert and reload the dashboard.
#
# tailscale cert issues a 90-day cert with NO auto-renewal, so a cron job runs
# this monthly (huge margin vs 90 days). Two kinds of node, one script:
#
#   host process  — the dashboard runs on the host and reads <repo>/data/tls.crt;
#                   the cert is issued straight into that file and the dashboard
#                   is restarted with start-dashboard.sh.
#   Compose node  — the dashboard runs in the `dashboard` container and reads
#                   /app/data/tls.crt from the autonomy-data volume (the host
#                   checkout has no data/tls.crt); the cert is issued into a
#                   private temp dir, streamed into the container as the
#                   autonomy user, logged in the volume, and the dashboard's
#                   own reload supervisor hands off to a worker that loads it
#                   (no restart: the vault stays warm, no connection drops).
#
# Installed crontab entry (monthly, 03:00 on the 1st), no environment needed:
#   0 3 1 * * "$AUTONOMY_ROOT/tools/dashboard/renew-tls-cert.sh"
# (AUTONOMY_ROOT defaults to the repo this script lives in. DASHBOARD_DOMAIN may
#  name the Tailnet host to issue for; without it the current certificate's own
#  .ts.net name is renewed. No machine name lives in this script.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${AUTONOMY_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
TAILSCALE="${TAILSCALE_BIN:-/usr/bin/tailscale}"
HOST_LOG="$REPO_ROOT/data/cert-renew.log"

# ── where the certificate lives ─────────────────────────────────────────────
# Explicit paths win; else the host checkout's data/tls.crt (a host-process
# node); else the Compose node's dashboard container: AUTONOMY_DASHBOARD_CONTAINER
# when the crontab pins it, otherwise the ONE running container labelled
# com.docker.compose.service=dashboard (dind and trial nodes share the label,
# so more than one is a refusal, never a guess).
CERT="${AUTONOMY_TLS_CERT:-}"
KEY="${AUTONOMY_TLS_KEY:-}"
CONTAINER=""
CONTAINER_REFUSAL=""
if [[ -z "$CERT" ]]; then
    if [[ -f "$REPO_ROOT/data/tls.crt" ]]; then
        CERT="$REPO_ROOT/data/tls.crt"
        KEY="${KEY:-$REPO_ROOT/data/tls.key}"
    elif [[ -n "${AUTONOMY_DASHBOARD_CONTAINER:-}" ]]; then
        CONTAINER="$AUTONOMY_DASHBOARD_CONTAINER"
    elif command -v docker >/dev/null 2>&1; then
        mapfile -t CANDIDATES < <(docker ps -q --filter "label=com.docker.compose.service=dashboard" 2>/dev/null || true)
        if [[ ${#CANDIDATES[@]} -eq 1 ]]; then
            CONTAINER="${CANDIDATES[0]}"
        elif [[ ${#CANDIDATES[@]} -gt 1 ]]; then
            CONTAINER_REFUSAL="${#CANDIDATES[@]} running dashboard containers (${CANDIDATES[*]}); pin AUTONOMY_DASHBOARD_CONTAINER in the crontab"
        fi
    fi
fi

# The log lives beside the certificate: the host file for a host-process node,
# /app/data/cert-renew.log in the data volume for a Compose node. It is opened
# before anything can refuse, so a refusal is always on record.
log() {
    if [[ -n "$CONTAINER" ]]; then
        docker exec -i -u autonomy "$CONTAINER" sh -c 'cat >> /app/data/cert-renew.log' <<<"$*"
    fi
    echo "$*"
}
mkdir -p "$(dirname "$HOST_LOG")" 2>/dev/null || true
exec >>"$HOST_LOG" 2>&1

# The Tailnet name in an existing certificate's subjectAltName, if any (the
# same rule the dashboard's remote-access seed uses).
tailnet_name_from_san() {  # reads openssl's subjectAltName text on stdin
    tr ',' '\n' | sed -n 's/.*DNS:\([A-Za-z0-9.-]*\.ts\.net\).*/\1/p' | head -1 | tr 'A-Z' 'a-z'
}
tailnet_name_from_cert() {  # tailnet_name_from_cert <cert path>
    [[ -f "$1" ]] || return 1
    openssl x509 -in "$1" -noout -ext subjectAltName 2>/dev/null | tailnet_name_from_san
}
tailnet_name_from_container() {  # tailnet_name_from_container <container>
    docker exec -u autonomy "$1" openssl x509 -in /app/data/tls.crt -noout -ext subjectAltName 2>/dev/null \
        | tailnet_name_from_san
}

DOMAIN="${DASHBOARD_DOMAIN:-}"
if [[ -z "$DOMAIN" ]]; then
    if [[ -n "$CONTAINER" ]]; then
        DOMAIN="$(tailnet_name_from_container "$CONTAINER" || true)"
    elif [[ -n "$CERT" ]]; then
        DOMAIN="$(tailnet_name_from_cert "$CERT" || true)"
    fi
fi
if [[ -n "$CONTAINER_REFUSAL" ]]; then
    log "!!! $(date -Is) $CONTAINER_REFUSAL; nothing renewed"
    exit 2
fi
if [[ -z "$CERT" && -z "$CONTAINER" ]]; then
    log "!!! $(date -Is) no certificate found: no AUTONOMY_TLS_CERT, no $REPO_ROOT/data/tls.crt, no running dashboard container; nothing renewed"
    exit 2
fi
if [[ -z "$DOMAIN" ]]; then
    log "!!! $(date -Is) no DASHBOARD_DOMAIN and no .ts.net name in the current certificate (${CERT:-container $CONTAINER}); nothing renewed"
    exit 2
fi
log "=== $(date -Is) renewing TLS cert for $DOMAIN (${CONTAINER:+compose container $CONTAINER}${CERT:+$CERT}) ==="

# ── issue ───────────────────────────────────────────────────────────────────
if [[ -n "$CONTAINER" ]]; then
    WORK="$(mktemp -d)"
    chmod 0700 "$WORK"
    trap 'rm -rf "$WORK"' EXIT
    ISSUE_CERT="$WORK/tls.crt"; ISSUE_KEY="$WORK/tls.key"
else
    ISSUE_CERT="$CERT"; ISSUE_KEY="$KEY"
fi
rc=0
"$TAILSCALE" cert --cert-file "$ISSUE_CERT" --key-file "$ISSUE_KEY" "$DOMAIN" || rc=$?
if [[ $rc -ne 0 ]]; then
    log "!!! $(date -Is) tailscale cert FAILED (rc=$rc) — existing cert left in place"
    exit "$rc"
fi

# ── install + reload ────────────────────────────────────────────────────────
if [[ -n "$CONTAINER" ]]; then
    # The pair must belong together before either file moves into place: a
    # certificate over one key and a key for another would take the listener
    # down at the next hand-off.
    if [[ "$(openssl x509 -in "$ISSUE_CERT" -noout -pubkey 2>/dev/null)" != "$(openssl pkey -in "$ISSUE_KEY" -pubout 2>/dev/null)" ]] \
            || [[ -z "$(openssl x509 -in "$ISSUE_CERT" -noout -pubkey 2>/dev/null)" ]]; then
        log "!!! $(date -Is) issued certificate and key do not match; nothing installed"
        exit 3
    fi
    # Streamed into the data volume AS the autonomy user (no root exec, no
    # docker cp's root-owned files), key private from the first byte; then the
    # key moves before the certificate so the served pair is never mismatched.
    # The dashboard's reload supervisor notices the certificate change and
    # hands off to a worker that loads the new pair: no restart, the vault
    # stays warm and no connection drops.
    docker exec -i -u autonomy "$CONTAINER" sh -c 'umask 077; cat > /app/data/tls.key.new' <"$ISSUE_KEY"
    docker exec -i -u autonomy "$CONTAINER" sh -c 'umask 022; cat > /app/data/tls.crt.new' <"$ISSUE_CERT"
    docker exec -u autonomy "$CONTAINER" sh -c 'mv /app/data/tls.key.new /app/data/tls.key && mv /app/data/tls.crt.new /app/data/tls.crt'
    log "cert installed in the data volume; the dashboard hands off to a worker with the new pair"
else
    echo "cert written; restarting dashboard to load it"
    "$REPO_ROOT/tools/dashboard/start-dashboard.sh" --restart
fi
log "=== $(date -Is) renewal OK ==="
