#!/usr/bin/env bash
# Start an Autonomy node from signed, published images: nothing is built here.
#
#   curl -fsSLO <this file>; bash install-published.sh --lock <image-lock.env URL or path>
#
# Rerun with a newer release's lock to upgrade the node in place: data,
# identity, docker-compose.override.yml and every .env value except the image
# references are kept, and the autonomy-code volume moves to the commit in the
# release's node image (refused when the volume has uncommitted changes or its
# HEAD is not an ancestor of that commit).
#
# Options:
#   --lock URL|PATH      the release's image lock (required): image@sha256 digests
#   --dir PATH           working directory for compose files and .env (default ~/autonomy)
#   --port N             dashboard port (default: the .env value, else 8080)
#   --host-home PATH     the operator's home, where existing Claude/Codex/Grok
#                        sign-ins are found (default: the .env value, else the
#                        invoking user's home, also under sudo; never /root
#                        unless root is the user)
#   --install-docker     install Docker Engine + Compose from docker.com when absent
#                        (apt; runs as root when invoked as root, else through sudo)
#   --yes                do not pause for confirmation before mutating steps
#   --http-port N        plain-HTTP first-screen port on localhost (default: the first free
#                        of 80, 8088, 8089; recorded in .env as DASHBOARD_HTTP_PORT)
#   --allow-downgrade    move the code volume to the release commit even when its
#                        HEAD is not an ancestor of it (a downgrade, local commits)
#
# AUTONOMY_COSIGN_BIN=/path/to/cosign uses an existing cosign instead of the
# pinned download (air-gapped hosts, tests); the embedded key is used either way.
#
# Trust: every image digest in the lock is verified with cosign against the
# project public key embedded below (a copy of deploy/cosign.pub) BEFORE it is
# pulled into use. The lock itself needs no trust: it only names digests, and
# an unsigned digest is refused. The compose file comes out of the verified
# node image, so this script is the only file fetched outside the registry.
set -euo pipefail

PROJECT_PUBLIC_KEY='-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE/hvkp7eAFQTMIW0NIn99LiAehHvC
FhD37yhGPNRtFYRUsfPaUTKgMHcy6dfwP4p7yprBRlpXj3EbONR3Ra9u+w==
-----END PUBLIC KEY-----'
COSIGN_VERSION=v3.1.3
COSIGN_SHA256_AMD64=4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71
COSIGN_SHA256_ARM64=c5d324e091826b0d7a78eb16fef316450b4eb9aaec045611c08ba06f5e73220a

LOCK="" DIR="$HOME/autonomy" PORT="" HTTP_PORT="" INSTALL_DOCKER=0 YES=0 HOST_HOME="" ALLOW_DOWNGRADE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --lock) LOCK="$2"; shift 2 ;;
        --dir) DIR="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --http-port) HTTP_PORT="$2"; shift 2 ;;
        --install-docker) INSTALL_DOCKER=1; shift ;;
        --yes) YES=1; shift ;;
        --host-home) HOST_HOME="$2"; shift 2 ;;
        --allow-downgrade) ALLOW_DOWNGRADE=1; shift ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$LOCK" ]] || { echo "--lock is required" >&2; exit 2; }

# An existing .env is a node being upgraded: a value it already holds is kept
# unless its flag is passed again, so an upgrade never moves the dashboard
# back to 8080 or points it at another home.
env_value() { [[ -f "$DIR/.env" ]] && sed -n "s/^$1=//p" "$DIR/.env" | tail -1; }
[[ -n "$PORT" ]] || PORT="$(env_value DASHBOARD_PORT || true)"
PORT="${PORT:-8080}"
[[ -n "$HOST_HOME" ]] || HOST_HOME="$(env_value AUTONOMY_HOST_HOME || true)"

# The operator's home is where the Welcome page's sign-in scan looks for an
# existing Claude, Codex or Grok sign-in. Under sudo, $HOME is root's home,
# which holds none of the operator's sign-ins and which the dashboard (uid
# 1000 in its container) cannot read, so the invoking user's home is used.
operator_home() {
    if [[ -n "$HOST_HOME" ]]; then printf '%s\n' "$HOST_HOME"; return; fi
    if [[ $(id -u) -eq 0 && -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
        getent passwd "$SUDO_USER" | cut -d: -f6; return
    fi
    printf '%s\n' "$HOME"
}
HOST_HOME="$(operator_home)"
[[ -d "$HOST_HOME" ]] || { echo "operator home $HOST_HOME does not exist (use --host-home)" >&2; exit 2; }
# The dashboard reads the home as uid 1000. Say so now when it cannot, instead
# of the Welcome page later reporting that no sign-in exists.
home_uid="$(stat -c %u "$HOST_HOME")"; home_mode="$(stat -c %a "$HOST_HOME")"
if [[ "$home_uid" != 1000 && $(( 8#$home_mode & 5 )) -ne 5 ]]; then
    echo "warning: the dashboard runs as uid 1000 and cannot read $HOST_HOME (owner uid $home_uid, mode $home_mode);" >&2
    echo "         sign-ins there will not be found. Use --host-home with your own home." >&2
fi

T0=$(date +%s)
step() { printf '==> [%4ss] %s\n' "$(( $(date +%s) - T0 ))" "$*"; }
confirm() {
    [[ $YES -eq 1 ]] && return 0
    read -r -p "$1 [y/N] " answer
    [[ "$answer" == y || "$answer" == Y ]]
}
as_root() { if [[ $(id -u) -eq 0 ]]; then "$@"; else sudo "$@"; fi; }

# ── 1. Docker ────────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    [[ $INSTALL_DOCKER -eq 1 ]] || {
        echo "Docker Engine + Compose are required (re-run with --install-docker on Ubuntu/Debian)." >&2
        exit 1
    }
    confirm "Install Docker Engine + Compose from docker.com?" || exit 1
    step "installing Docker Engine"
    # shellcheck disable=SC1091
    . /etc/os-release
    as_root apt-get update -y -qq
    as_root apt-get install -y -qq ca-certificates curl gnupg >/dev/null
    as_root install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" | as_root gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    as_root chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        | as_root tee /etc/apt/sources.list.d/docker.list >/dev/null
    as_root apt-get update -y -qq
    as_root apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
    as_root systemctl enable --now docker >/dev/null 2>&1 || as_root service docker start
    if [[ $(id -u) -ne 0 ]]; then
        as_root usermod -aG docker "$USER"
        if ! docker ps >/dev/null 2>&1; then
            echo "Docker is installed. Open a new shell (docker group membership) and re-run this same command." >&2
            exit 3
        fi
    fi
fi
# The node's Compose files mount two directories of the autonomy-code volume by
# subpath (volume: subpath:, auto-8pohz), which needs Docker Engine API 1.45
# (Engine 26) or later. Name the floor here rather than let `compose up` fail
# with an interpolation error on an older daemon.
DOCKER_API_FLOOR=1.45
docker_api_version() { docker version --format '{{.Server.APIVersion}}' 2>/dev/null; }
require_docker_api() {  # require_docker_api <min-api> ; e.g. 1.45
    local have
    have="$(docker_api_version)"
    if [[ -z "$have" ]]; then
        echo "could not read the Docker Engine API version (is the daemon running and reachable?)" >&2
        return 1
    fi
    if [[ "$(printf '%s\n%s\n' "$1" "$have" | sort -V | head -1)" != "$1" ]]; then
        echo "Docker Engine API $have is too old: this node needs API $1 or later (Docker Engine 26+) for volume subpath mounts." >&2
        return 1
    fi
}
require_docker_api "$DOCKER_API_FLOOR" || exit 8
docker ps >/dev/null || { echo "docker is installed but not usable by $(id -un)" >&2; exit 1; }

# ── 2. cosign, pinned by checksum ────────────────────────────────────────────
TOOLS="$(mktemp -d)"
trap 'rm -rf "$TOOLS"' EXIT
case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64; COSIGN_SHA256=$COSIGN_SHA256_AMD64 ;;
    aarch64|arm64) ARCH=arm64; COSIGN_SHA256=$COSIGN_SHA256_ARM64 ;;
    *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
if [[ -n "${AUTONOMY_COSIGN_BIN:-}" ]]; then
    cp "$AUTONOMY_COSIGN_BIN" "$TOOLS/cosign"
else
    step "fetching cosign $COSIGN_VERSION"
    curl -fsSL -o "$TOOLS/cosign" \
        "https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-${ARCH}"
    echo "${COSIGN_SHA256}  $TOOLS/cosign" | sha256sum -c --quiet -
fi
chmod +x "$TOOLS/cosign"
printf '%s\n' "$PROJECT_PUBLIC_KEY" >"$TOOLS/cosign.pub"

# ── 3. Lock ──────────────────────────────────────────────────────────────────
if [[ "$LOCK" == http://* || "$LOCK" == https://* ]]; then
    curl -fsSL -o "$TOOLS/image-lock.env" "$LOCK"
else
    cp "$LOCK" "$TOOLS/image-lock.env"
fi
declare -A IMG=()
RELEASE_TAG=""
while IFS='=' read -r name value; do
    case "$name" in
        AUTONOMY_RELEASE_TAG) RELEASE_TAG="$value" ;;
        AUTONOMY_*_IMAGE)
            [[ "$value" =~ ^[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}$ ]] || {
                echo "refusing non-digest lock entry: $name=$value" >&2; exit 2; }
            IMG[$name]="$value" ;;
    esac
done <"$TOOLS/image-lock.env"
for need in AUTONOMY_NODE_IMAGE AUTONOMY_SESSION_IMAGE AUTONOMY_SESSION_PLATFORM_IMAGE AUTONOMY_SESSION_DIND_IMAGE; do
    [[ -n "${IMG[$need]:-}" ]] || { echo "lock is missing $need" >&2; exit 2; }
done
step "release ${RELEASE_TAG:-?}"

# ── 4. Verify every signature, then pull ─────────────────────────────────────
for name in "${!IMG[@]}"; do
    ref="${IMG[$name]}"
    "$TOOLS/cosign" verify --insecure-ignore-tlog --key "$TOOLS/cosign.pub" "$ref" >/dev/null 2>"$TOOLS/verify.err" || {
        # cosign fetches the signature from the registry first: a registry
        # that refuses the anonymous pull (a package left private, a wrong
        # name) fails here too, and that is not a signature failure. Windows
        # run 6 (2026-09-27) read a private GHCR package as a bad signature.
        # Registry forms only: cosign's own "no matching signatures" or
        # "signature not found" is a signature failure and stays exit 4.
        if grep -Eqi 'UNAUTHORIZED|unauthorized|denied|MANIFEST_UNKNOWN|NAME_UNKNOWN|manifest unknown|repository name not known|no such host|could not resolve' "$TOOLS/verify.err"; then
            echo "IMAGE UNREACHABLE: $ref cannot be pulled anonymously (private package or wrong reference) — refusing to install" >&2
            sed 's/^/    /' "$TOOLS/verify.err" >&2
            echo "    A first-time GHCR package is private: make it public in the package's settings, then rerun." >&2
            exit 9
        fi
        echo "SIGNATURE CHECK FAILED for $ref — refusing to install" >&2
        sed 's/^/    /' "$TOOLS/verify.err" >&2
        exit 4
    }
    step "signature verified: $ref"
done
confirm "Pull the verified images and start Autonomy in $DIR?" || exit 1
for name in "${!IMG[@]}"; do
    step "pulling ${IMG[$name]}"
    docker pull -q "${IMG[$name]}" >/dev/null
done

# ── 5. Code volume to the release commit (an existing node only) ─────────────
# The image's ENTRYPOINT and CMD run from the autonomy-code volume, which Docker
# seeds from the image only while it is empty: a new image over an existing
# volume runs the old code. So the volume is moved to the commit the release's
# node image carries. A one-shot container of the new image does it as
# autonomy (uid 1000, the volume's owner; root would leave root-owned files
# behind), with the volume at a second path: fetch the image's /app commit
# into the volume's repository, then `git reset --keep`, which never discards
# a change. It runs twice: first only the checks, so a refusal comes before
# anything the node uses has changed (its code, its session image tags, $DIR);
# then, with the node's containers stopped, the move, because the dashboard
# hot-reloads from the volume and would otherwise run the new code on the old
# image until `compose up` recreates it.
CODE_VOLUME=autonomy-code
code_step() {  # code_step check|move ; prints "<old HEAD> <release commit>"
    docker run --rm -i --user 1000:1000 --network none -e HOME=/tmp \
        -e AUTONOMY_CODE_STEP="$1" -e AUTONOMY_ALLOW_DOWNGRADE="$ALLOW_DOWNGRADE" \
        -v "$CODE_VOLUME:/volume" --entrypoint sh "${IMG[AUTONOMY_NODE_IMAGE]}" -s <<'CODE'
set -eu
release=/app code=/volume
want="$(sed -n 's/^commit=//p' "$release/VERSION")"
[ -n "$want" ] || { echo "the release node image names no commit in /app/VERSION" >&2; exit 1; }
cd "$code"
have="$(git rev-parse -q --verify HEAD)" || { echo "the autonomy-code volume holds no git repository" >&2; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "refusing to upgrade: the autonomy-code volume has uncommitted changes (commit or discard them in the dashboard container's /app first)" >&2
    exit 1
fi
git fetch -q "$release" HEAD
[ "$(git rev-parse FETCH_HEAD)" = "$want" ] || { echo "the release node image's /app HEAD is not the commit in its /app/VERSION" >&2; exit 1; }
if ! git merge-base --is-ancestor HEAD "$want" && [ "$AUTONOMY_ALLOW_DOWNGRADE" != 1 ]; then
    echo "refusing to upgrade: the autonomy-code volume's HEAD $have is not an ancestor of release commit $want (a downgrade, or local commits); --allow-downgrade overrides" >&2
    exit 1
fi
if [ "$AUTONOMY_CODE_STEP" = move ]; then
    git reset -q --keep "$want"
    cp "$release/VERSION" "$code/VERSION"
fi
echo "$have $want"
CODE
}
if docker volume inspect "$CODE_VOLUME" >/dev/null 2>&1; then
    step "checking the $CODE_VOLUME volume against the release commit"
    code_step check >/dev/null || exit 10
    # Every container of the node's Compose project (name: autonomy in
    # docker-compose.yml), found by label so a node whose project directory
    # is elsewhere (built from a checkout) is stopped too.
    running="$(docker ps -q --filter label=com.docker.compose.project=autonomy)"
    if [[ -n "$running" ]]; then
        step "stopping the node's containers"
        # shellcheck disable=SC2086
        docker stop $running >/dev/null
    fi
    step "moving the $CODE_VOLUME volume to the release commit"
    moved="$(code_step move)" || {
        echo "the node is stopped and its code volume was not moved; nothing else changed: 'docker compose up -d' in the node's Compose project directory restarts the previous release" >&2
        exit 10
    }
    step "code volume: ${moved% *} -> ${moved#* }"
    # From here a failure leaves the node stopped on the release's code: say
    # how to finish, so the operator does not have to guess. A rerun passes
    # the code checks (HEAD is the release commit) and picks up where this
    # one stopped.
    trap 'rc=$?; rm -rf "$TOOLS"
          [[ $rc -eq 0 ]] || echo "the node may be stopped on the new release'"'"'s code: rerun this same command to finish the upgrade" >&2' EXIT
fi

# The session launcher starts sessions from these local names.
docker tag "${IMG[AUTONOMY_SESSION_IMAGE]}" autonomy-session
docker tag "${IMG[AUTONOMY_SESSION_PLATFORM_IMAGE]}" autonomy-session-platform
docker tag "${IMG[AUTONOMY_SESSION_DIND_IMAGE]}" autonomy-session-dind
# Releases cut after the in-node host terminal (deploy/publish-images.sh)
# carry its image; a release whose node predates it has none and needs none.
if [[ -n "${IMG[AUTONOMY_HOST_TERMINAL_IMAGE]:-}" ]]; then
    docker tag "${IMG[AUTONOMY_HOST_TERMINAL_IMAGE]}" autonomy-host-terminal
fi

# ── 6. Compose file from the verified node image ─────────────────────────────
mkdir -p "$DIR"
cid="$(docker create "${IMG[AUTONOMY_NODE_IMAGE]}")"
docker cp "$cid:/app/docker-compose.yml" "$DIR/docker-compose.yml"
docker rm "$cid" >/dev/null
cd "$DIR"
touch .env
set_env() { grep -q "^$1=" .env && sed -i "s|^$1=.*|$1=$2|" .env || echo "$1=$2" >>.env; }
# The plain-HTTP first-screen port: the first free of the candidates, chosen once
# and recorded in .env (docker-compose.yml publishes it on 127.0.0.1). A connect
# probe on localhost is deterministic and needs no tool beyond bash.
# WSL detection that survives sudo: env_reset drops WSL_DISTRO_NAME and secure_path
# drops /mnt/c from PATH, so the kernel release and the absolute powershell path are
# consulted too. wsl_powershell prints the powershell.exe path; rc 1 means the
# Windows-side checks are skipped (the final check says so when this IS WSL).
is_wsl() { [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft "${WSL_OSRELEASE_FILE:-/proc/sys/kernel/osrelease}" 2>/dev/null; }
wsl_powershell() {
    is_wsl || return 1
    local candidate
    for candidate in "$(command -v powershell.exe 2>/dev/null || true)" \
                     /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe; do
        [[ -n "$candidate" && -x "$candidate" ]] && { echo "$candidate"; return 0; }
    done
    return 1
}
port_is_free() {
    ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || return 1
    # Under WSL the distro's localhost is relayed from Windows, and a port Windows
    # itself holds (HTTP.sys on 80) is busy there while nothing answers in here.
    local ps win
    if ps="$(wsl_powershell)"; then
        win="$("$ps" -NoProfile -NonInteractive -Command \
            "if (Get-NetTCPConnection -State Listen -LocalPort $1 -ErrorAction SilentlyContinue) { 'busy' } else { 'free' }" \
            2>/dev/null | tr -d '\r')"
        [[ "$win" == busy ]] && return 1
    fi
    return 0
}
windows_first_screen_check() {  # windows_first_screen_check <http-port> <https-port>
    local ps code
    is_wsl || return 0
    if ! ps="$(wsl_powershell)"; then
        echo "NOTE: this is WSL but powershell.exe was not found (sudo drops /mnt/c from PATH), so the Windows-side" >&2
        echo "      port checks were skipped. Confirm http://localhost:$1/ opens from a Windows browser." >&2
        return 0
    fi
    code="$("$ps" -NoProfile -NonInteractive -Command \
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
    if [[ -n "$requested" ]]; then set_env DASHBOARD_HTTP_PORT "$requested"; echo "$requested"; return 0; fi
    if grep -q '^DASHBOARD_HTTP_PORT=' .env; then
        sed -n 's/^DASHBOARD_HTTP_PORT=//p' .env | tail -1; return 0
    fi
    for p in "$@"; do
        if port_is_free "$p"; then set_env DASHBOARD_HTTP_PORT "$p"; echo "$p"; return 0; fi
    done
    return 1
}
set_env AUTONOMY_IMAGE "${IMG[AUTONOMY_NODE_IMAGE]}"
# The Service gateway is started by the dashboard itself, from a Compose run
# inside its container where this .env is not read: docker-compose.yml passes
# the value into the dashboard's environment. A release cut before the gateway
# was published (2026.09.26) has none; its gateway cannot start until updated.
if [[ -n "${IMG[AUTONOMY_SERVICE_GATEWAY_IMAGE]:-}" ]]; then
    set_env AUTONOMY_SERVICE_GATEWAY_IMAGE "${IMG[AUTONOMY_SERVICE_GATEWAY_IMAGE]}"
fi
set_env AUTONOMY_HOST_HOME "$HOST_HOME"
set_env DASHBOARD_PORT "$PORT"
HTTP_PORT="$(choose_http_port "$HTTP_PORT" 80 8088 8089)" || {
    echo "ports 80, 8088 and 8089 are all in use on localhost; pass --http-port N" >&2; exit 7; }
if ! grep -q '^AUTONOMY_SUBNET=' .env; then
    step "network preflight (choosing a free subnet)"
    # Host network namespace for the host's routes; the Docker socket so the
    # networks Docker already holds are avoided too.
    subnet_line="$(docker run --rm --network host \
        -v /var/run/docker.sock:/var/run/docker.sock \
        --entrypoint python3 "${IMG[AUTONOMY_NODE_IMAGE]}" \
        -m tools.network.network_preflight --env)" || {
        echo "network preflight could not choose a safe subnet; set AUTONOMY_SUBNET in $DIR/.env" >&2
        exit 6
    }
    echo "$subnet_line" >>.env
fi

# ── 7. Start and wait for a real answer ──────────────────────────────────────
step "starting the node"
docker compose up -d --no-build --quiet-pull
for _ in $(seq 1 "${AUTONOMY_READY_TIMEOUT:-180}"); do
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 "https://localhost:${PORT}/api/ping" || true)"
    [[ "$code" == 200 ]] && break
    sleep 1
done
[[ "${code:-}" == 200 ]] || { echo "the dashboard did not answer /api/ping with 200 (last: $code)" >&2; docker compose ps >&2; exit 5; }
for _ in $(seq 1 30); do
    plain="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:${HTTP_PORT}/api/ping" || true)"
    [[ "$plain" == 200 ]] && break
    sleep 1
done
[[ "${plain:-}" == 200 ]] || { echo "the plain-HTTP listener did not answer http://localhost:${HTTP_PORT}/api/ping with 200 (last: $plain)" >&2; docker compose ps >&2; exit 5; }
windows_first_screen_check "$HTTP_PORT" "$PORT"
step "dashboard is up"

# ── 8. Record the installed release in the data volume ───────────────────────
# release/installed.env is the lock this node now runs plus when it was
# installed; the one it replaces moves to release/history/. Written only once
# the dashboard answers, so it never names a release that did not start.
INSTALLED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker run --rm -i --user 1000:1000 --network none -e INSTALLED_AT="$INSTALLED_AT" \
    -v autonomy-data:/data --entrypoint sh "${IMG[AUTONOMY_NODE_IMAGE]}" -c '
set -eu
d=/data/release
mkdir -p "$d/history"
if [ -f "$d/installed.env" ]; then
    was="$(sed -n "s/^AUTONOMY_INSTALLED_AT=//p" "$d/installed.env" | tr -d :)"
    mv "$d/installed.env" "$d/history/${was:-unknown-$INSTALLED_AT}.env"
fi
{ cat; echo "AUTONOMY_INSTALLED_AT=$INSTALLED_AT"; } >"$d/installed.env.tmp"
mv "$d/installed.env.tmp" "$d/installed.env"' <"$TOOLS/image-lock.env" \
    || echo "warning: could not record the installed release in the autonomy-data volume (release/installed.env)" >&2
echo
echo "Autonomy ${RELEASE_TAG} is running: open http://localhost:${HTTP_PORT}/"
echo "  (from another machine: https://<this-host>:${PORT}/ — self-signed certificate, accept once)"
echo "Total time: $(( $(date +%s) - T0 )) s"
