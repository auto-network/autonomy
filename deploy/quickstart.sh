#!/usr/bin/env bash
# One command from a bare box to a running Autonomy node.
#
#   deploy/quickstart.sh --first-org myorg                     # found a new identity's org
#   deploy/quickstart.sh --fleet-invite "$FLEET_INVITATION"    # join the operator's personal fleet
#   deploy/quickstart.sh --org-invite "$INVITATION"            # join an existing organization
#   deploy/quickstart.sh --bare                                # boot with no role; claim an
#                                                              # invitation later in the browser
#
# Options:
#   --source URL|PATH        git source to clone (default: the checkout this script is in)
#   --dir PATH               checkout/working directory (default: ~/autonomy, or this checkout)
#   --data-root PATH         keep data at a host path (deploy/docker-compose.host-data.yml)
#   --port N                 dashboard port (default 8080)
#   --http-port N            plain-HTTP first-screen port on localhost (default: the first
#                            free of 80, 8088, 8089; recorded in .env as DASHBOARD_HTTP_PORT)
#   --direct-advertise URL   ws://<reachable-ip>:9410 — bind the fleet direct listener in the
#                            connector, map the port, and advertise it (fleet machines)
#   --claude-token-file F    long-lived Claude setup token; installed to ~/.claude/.setup-token
#                            (fleet machines receive their configuration by sync and need none)
#   --install-docker         install Docker Engine + Compose from docker.com (apt; needs sudo)
#   --yes                    do not pause for confirmation before the mutating steps
#
# Everything this does is what deploy/install/INSTALL.md Path A does by hand: preflight,
# pin a free subnet in .env, build the service gateway, compose up, wait for health.
# Identity ceremonies stay in the operator's browser; this script never handles a
# passphrase, recovery code, or root key.
set -euo pipefail

ROLE_KIND="" ROLE_VALUE="" SOURCE="" DIR="" DATA_ROOT="" PORT=8080 HTTP_PORT="" DIRECT="" TOKEN_FILE=""
INSTALL_DOCKER=0 YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --first-org) ROLE_KIND=first-org; ROLE_VALUE="$2"; shift 2 ;;
        --fleet-invite) ROLE_KIND=fleet; ROLE_VALUE="$2"; shift 2 ;;
        --org-invite) ROLE_KIND=org; ROLE_VALUE="$2"; shift 2 ;;
        --bare) ROLE_KIND=bare; ROLE_VALUE=""; shift ;;
        --source) SOURCE="$2"; shift 2 ;;
        --dir) DIR="$2"; shift 2 ;;
        --data-root) DATA_ROOT="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --http-port) HTTP_PORT="$2"; shift 2 ;;
        --direct-advertise) DIRECT="$2"; shift 2 ;;
        --claude-token-file) TOKEN_FILE="$2"; shift 2 ;;
        --install-docker) INSTALL_DOCKER=1; shift ;;
        --yes) YES=1; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$ROLE_KIND" ]] || { echo "one of --first-org / --fleet-invite / --org-invite / --bare is required" >&2; exit 2; }

confirm() {
    [[ $YES -eq 1 ]] && return 0
    read -r -p "$1 [y/N] " answer
    [[ "$answer" == y || "$answer" == Y ]]
}

# ── 0. Docker ────────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    if [[ $INSTALL_DOCKER -eq 1 ]]; then
        confirm "Install Docker Engine + Compose from docker.com (sudo apt)?" || exit 1
        sudo apt-get update -y
        sudo apt-get install -y ca-certificates curl gnupg
        sudo install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        sudo chmod a+r /etc/apt/keyrings/docker.gpg
        # shellcheck disable=SC1091
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
            | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
        sudo apt-get update -y
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        sudo usermod -aG docker "$USER" || true
        echo "Docker installed. If 'docker ps' is refused, log out and in (group membership) and re-run."
    else
        echo "Docker Engine + Compose plugin are required (re-run with --install-docker on Ubuntu)." >&2
        exit 1
    fi
fi

# ── 1. Checkout ──────────────────────────────────────────────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "$DIR" ]]; then
    if [[ -f "$HERE/docker-compose.yml" && -z "$SOURCE" ]]; then DIR="$HERE"; else DIR="$HOME/autonomy"; fi
fi
if [[ ! -f "$DIR/docker-compose.yml" ]]; then
    [[ -n "$SOURCE" ]] || SOURCE="$HERE"
    echo "==> cloning $SOURCE -> $DIR"
    git clone "$SOURCE" "$DIR"
fi
cd "$DIR"
echo "==> checkout: $DIR @ $(git rev-parse --short HEAD 2>/dev/null || echo '?')"

# The plain-HTTP first-screen port: the first free of the candidates, chosen once
# and recorded in .env (docker-compose.yml publishes it on 127.0.0.1). A connect
# probe on localhost is deterministic and needs no tool beyond bash.
port_is_free() {
    ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || return 1
    # Under WSL the distro's localhost is relayed from Windows, and a port Windows
    # itself holds (HTTP.sys on 80) is busy there while nothing answers in here.
    if [[ -n "${WSL_DISTRO_NAME:-}" ]] && command -v powershell.exe >/dev/null 2>&1; then
        local win
        win="$(powershell.exe -NoProfile -NonInteractive -Command \
            "if (Get-NetTCPConnection -State Listen -LocalPort $1 -ErrorAction SilentlyContinue) { 'busy' } else { 'free' }" \
            2>/dev/null | tr -d '\r')"
        [[ "$win" == busy ]] && return 1
    fi
    return 0
}
windows_first_screen_check() {  # windows_first_screen_check <http-port> <https-port>
    [[ -n "${WSL_DISTRO_NAME:-}" ]] && command -v powershell.exe >/dev/null 2>&1 || return 0
    local code
    code="$(powershell.exe -NoProfile -NonInteractive -Command \
        "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://localhost:$1/api/ping).StatusCode } catch { 0 }" \
        2>/dev/null | tr -d '\r')"
    if [[ "$code" == 200 ]]; then
        echo "==> Windows reaches http://localhost:$1 (200)"
    else
        echo "WARNING: Windows could not reach http://localhost:$1/api/ping (got '${code:-none}')." >&2
        echo "         Open https://localhost:$2/ from the Windows browser instead, and check that Docker's" >&2
        echo "         userland proxy is enabled and that nothing on Windows holds port $1." >&2
    fi
}
choose_http_port() {  # choose_http_port <requested-or-empty> <candidate>... ; echoes the port
    local requested="$1" p; shift
    if [[ -n "$requested" ]]; then
        grep -q '^DASHBOARD_HTTP_PORT=' .env && sed -i "s|^DASHBOARD_HTTP_PORT=.*|DASHBOARD_HTTP_PORT=$requested|" .env \
            || echo "DASHBOARD_HTTP_PORT=$requested" >> .env
        echo "$requested"; return 0
    fi
    if grep -q '^DASHBOARD_HTTP_PORT=' .env; then
        sed -n 's/^DASHBOARD_HTTP_PORT=//p' .env | tail -1; return 0
    fi
    for p in "$@"; do
        if port_is_free "$p"; then echo "DASHBOARD_HTTP_PORT=$p" >> .env; echo "$p"; return 0; fi
    done
    return 1
}

# ── 2. Preflight + .env ──────────────────────────────────────────────────────
touch .env
grep -q '^AUTONOMY_SUBNET=' .env || {
    echo "==> network preflight (chooses a free subnet)"
    python3 -m tools.network.network_preflight || true
    python3 -m tools.network.network_preflight --env >> .env
}
grep -q '^AUTONOMY_HOST_HOME=' .env || echo "AUTONOMY_HOST_HOME=$HOME" >> .env
grep -q '^DASHBOARD_PORT=' .env || echo "DASHBOARD_PORT=$PORT" >> .env
HTTP_PORT="$(choose_http_port "$HTTP_PORT" 80 8088 8089)" || {
    echo "ports 80, 8088 and 8089 are all in use on localhost; pass --http-port N" >&2; exit 1; }
[[ -z "$DATA_ROOT" ]] || { grep -q '^AUTONOMY_HOST_DATA_ROOT=' .env || echo "AUTONOMY_HOST_DATA_ROOT=$DATA_ROOT" >> .env; }
echo "==> .env:"; sed 's/^/    /' .env

# ── 3. Inference token (new identities only) ─────────────────────────────────
if [[ -n "$TOKEN_FILE" ]]; then
    install -d -m 0700 "$HOME/.claude"
    install -m 0600 "$TOKEN_FILE" "$HOME/.claude/.setup-token"
    echo "==> Claude setup token installed at ~/.claude/.setup-token"
fi

# ── 4. Direct fleet listener (compose port mapping; the row is written after boot) ──
COMPOSE=(docker compose -f docker-compose.yml)
[[ -z "$DATA_ROOT" ]] || COMPOSE+=(-f deploy/docker-compose.host-data.yml)
if [[ -n "$DIRECT" ]]; then
    DIRECT_HOST="${DIRECT#*://}"; DIRECT_HOST="${DIRECT_HOST%%:*}"
    DIRECT_PORT="${DIRECT##*:}"
    cat > docker-compose.direct.yml <<YML
# Fleet direct tier: expose the connector-hosted listener on the reachable address.
services:
  dashboard:
    ports:
      - "${DIRECT_HOST}:${DIRECT_PORT}:${DIRECT_PORT}"
YML
    COMPOSE+=(-f docker-compose.direct.yml)
fi

# ── 5. Build + up ────────────────────────────────────────────────────────────
confirm "Build the service gateway and start the node in $DIR as role '$ROLE_KIND'?" || exit 1
"${COMPOSE[@]}" --profile service-gateway build service-gateway
case "$ROLE_KIND" in
    first-org) AUTONOMY_FIRST_ORG="$ROLE_VALUE" "${COMPOSE[@]}" up -d ;;
    fleet)     AUTONOMY_FLEET_INVITE="$ROLE_VALUE" "${COMPOSE[@]}" up -d ;;
    org)       AUTONOMY_INVITE="$ROLE_VALUE" "${COMPOSE[@]}" up -d ;;
    bare)      "${COMPOSE[@]}" up -d ;;
esac

# ── 6. Health ────────────────────────────────────────────────────────────────
echo -n "==> waiting for https://localhost:${PORT}/healthz "
for _ in $(seq 1 90); do
    if curl -sk --max-time 3 "https://localhost:${PORT}/healthz" >/dev/null 2>&1; then echo "up"; break; fi
    echo -n "."; sleep 2
done
echo -n "==> waiting for http://localhost:${HTTP_PORT}/healthz "
for _ in $(seq 1 30); do
    if curl -s --max-time 3 "http://localhost:${HTTP_PORT}/healthz" >/dev/null 2>&1; then echo "up"; break; fi
    echo -n "."; sleep 2
done
windows_first_screen_check "$HTTP_PORT" "$PORT"

# ── 7. Direct listener row (machine-local; the connector binds within 15 s of arming) ──
if [[ -n "$DIRECT" ]]; then
    CONTAINER="$("${COMPOSE[@]}" ps -q dashboard)"
    docker exec -u autonomy "$CONTAINER" python3 -c "
from tools.network import fleet_direct_config as f
f.store(f.FleetDirectConfig('0.0.0.0', ${DIRECT_PORT}, ('${DIRECT}',)))
print('fleet-direct row:', f.load())"
fi

cat <<TXT

Node is up:  http://localhost:${HTTP_PORT}   (first screen; this machine only, no certificate warning)
             https://<this-host>:${PORT}  (from other machines; self-signed certificate, accept once)
Next, in the operator's browser:
TXT
case "$ROLE_KIND" in
    first-org) echo "  Create the identity for org '$ROLE_VALUE' (password) on the welcome page; the token in ~/.claude/.setup-token serves inference." ;;
    fleet)     echo "  The page shows the machine comparison code; approve it from the parent dashboard's inbox. Settings, credentials, and inference config arrive by fleet sync." ;;
    org)       echo "  Create this machine's own identity (password), then the org invitation completes; approval may be asynchronous." ;;
    bare)      echo "  No role yet. Fleet: paste the fleet invitation at /network/join. Org: create an identity, then paste the org invitation." ;;
esac
