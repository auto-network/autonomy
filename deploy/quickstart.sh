#!/usr/bin/env bash
# One command from a bare box to a running Autonomy node.
#
#   deploy/quickstart.sh --first-org myorg                     # found a new identity's org
#   deploy/quickstart.sh --fleet-invite "$FLEET_INVITATION"    # join the operator's personal fleet
#   deploy/quickstart.sh --org-invite "$INVITATION"            # join an existing organization
#
# Options:
#   --source URL|PATH        git source to clone (default: the checkout this script is in)
#   --dir PATH               checkout/working directory (default: ~/autonomy, or this checkout)
#   --data-root PATH         keep data at a host path (deploy/docker-compose.host-data.yml)
#   --port N                 dashboard port (default 8080)
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

ROLE_KIND="" ROLE_VALUE="" SOURCE="" DIR="" DATA_ROOT="" PORT=8080 DIRECT="" TOKEN_FILE=""
INSTALL_DOCKER=0 YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --first-org) ROLE_KIND=first-org; ROLE_VALUE="$2"; shift 2 ;;
        --fleet-invite) ROLE_KIND=fleet; ROLE_VALUE="$2"; shift 2 ;;
        --org-invite) ROLE_KIND=org; ROLE_VALUE="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        --dir) DIR="$2"; shift 2 ;;
        --data-root) DATA_ROOT="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --direct-advertise) DIRECT="$2"; shift 2 ;;
        --claude-token-file) TOKEN_FILE="$2"; shift 2 ;;
        --install-docker) INSTALL_DOCKER=1; shift ;;
        --yes) YES=1; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$ROLE_KIND" ]] || { echo "one of --first-org / --fleet-invite / --org-invite is required" >&2; exit 2; }

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

# ── 2. Preflight + .env ──────────────────────────────────────────────────────
touch .env
grep -q '^AUTONOMY_SUBNET=' .env || {
    echo "==> network preflight (chooses a free subnet)"
    python3 -m tools.network.network_preflight || true
    python3 -m tools.network.network_preflight --env >> .env
}
grep -q '^AUTONOMY_HOST_HOME=' .env || echo "AUTONOMY_HOST_HOME=$HOME" >> .env
grep -q '^DASHBOARD_PORT=' .env || echo "DASHBOARD_PORT=$PORT" >> .env
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
esac

# ── 6. Health ────────────────────────────────────────────────────────────────
echo -n "==> waiting for https://localhost:${PORT}/healthz "
for _ in $(seq 1 90); do
    if curl -sk --max-time 3 "https://localhost:${PORT}/healthz" >/dev/null 2>&1; then echo "up"; break; fi
    echo -n "."; sleep 2
done

# ── 7. Direct listener row (machine-local; the connector binds within 15 s of arming) ──
if [[ -n "$DIRECT" ]]; then
    CONTAINER="$("${COMPOSE[@]}" ps -q dashboard)"
    docker exec -u autonomy "$CONTAINER" python3 -c "
from tools.network import fleet_direct_config as f
f.store(f.FleetDirectConfig('0.0.0.0', ${DIRECT_PORT}, ('${DIRECT}',)))
print('fleet-direct row:', f.load())"
fi

cat <<TXT

Node is up: https://<this-host>:${PORT}  (self-signed certificate; accept once)
Next, in the operator's browser:
TXT
case "$ROLE_KIND" in
    first-org) echo "  Create the identity for org '$ROLE_VALUE' (password) on the welcome page; the token in ~/.claude/.setup-token serves inference." ;;
    fleet)     echo "  The page shows the machine comparison code; approve it from the parent dashboard's inbox. Settings, credentials, and inference config arrive by fleet sync." ;;
    org)       echo "  Create this machine's own identity (password), then the org invitation completes; approval may be asynchronous." ;;
esac
