#!/usr/bin/env bash
# Build, publish, and digest-pin the Autonomy image family.
#
# Registry credentials are deliberately outside this script: authenticate with
# `docker login` first. The registry and namespace are caller-selected, so this
# release path does not replace the sovereign build-from-source path. Signing
# is a separate, operator-invoked human act; this script never handles a key.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

REGISTRY="${AUTONOMY_REGISTRY:?set AUTONOMY_REGISTRY (for example ghcr.io)}"
NAMESPACE="${AUTONOMY_IMAGE_NAMESPACE:?set AUTONOMY_IMAGE_NAMESPACE}"
RELEASE_TAG="${AUTONOMY_RELEASE_TAG:?set AUTONOMY_RELEASE_TAG}"
LOCK_FILE="${AUTONOMY_IMAGE_LOCK:-$REPO_ROOT/image-lock.env}"
AGENT_BUILD="${AUTONOMY_AGENT_BUILD_SCRIPT:-$REPO_ROOT/agents/build.sh}"

case "$REGISTRY" in
    *[!A-Za-z0-9._:-]*|"") echo "invalid AUTONOMY_REGISTRY" >&2; exit 2 ;;
esac
case "$NAMESPACE" in
    *[!A-Za-z0-9._/-]*|"") echo "invalid AUTONOMY_IMAGE_NAMESPACE" >&2; exit 2 ;;
esac
case "$RELEASE_TAG" in
    *[!A-Za-z0-9._-]*|"") echo "invalid AUTONOMY_RELEASE_TAG" >&2; exit 2 ;;
esac

command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }

# The repository the images come from, stamped as org.opencontainers.image.source
# on every image built here and by agents/build.sh. GHCR attaches a package to
# its repository by this label (and a first-time package then inherits the
# repository's visibility instead of being created private, which stopped
# Windows run 6 on 2026-09-27). Optional: a sovereign build may have no
# public repository to name.
SOURCE_URL="${AUTONOMY_IMAGE_SOURCE:-}"
source_label=()
if [[ -n "$SOURCE_URL" ]]; then
    case "$SOURCE_URL" in
        https://*) ;;
        *) echo "AUTONOMY_IMAGE_SOURCE must be an https URL (the repository the images come from)" >&2; exit 2 ;;
    esac
    source_label=(--label "org.opencontainers.image.source=$SOURCE_URL")
    export AUTONOMY_IMAGE_SOURCE="$SOURCE_URL"
fi

base="$REGISTRY/$NAMESPACE"
node_ref="$base/autonomy-node:$RELEASE_TAG"
session_ref="$base/autonomy-session:$RELEASE_TAG"
platform_ref="$base/autonomy-session-platform:$RELEASE_TAG"
dind_ref="$base/autonomy-session-dind:$RELEASE_TAG"
host_terminal_ref="$base/autonomy-host-terminal:$RELEASE_TAG"
service_gateway_ref="$base/autonomy-service-gateway:$RELEASE_TAG"

echo "==> Building node image from deploy/Dockerfile"
# Build from a throwaway clean clone. deploy/Dockerfile self-stamps /app/VERSION
# by reading the commit hash + date from the checkout's own .git; a linked
# worktree's .git is a pointer the build can't resolve, so cloning to a
# standalone repo (real .git dir, committed HEAD) lets the release self-stamp
# correctly no matter where this script is run. Uncommitted changes are excluded
# by design — a release is the committed state. --depth 1 keeps it cheap.
node_src="$(mktemp -d)"
git clone --quiet --depth 1 "file://$REPO_ROOT/.git" "$node_src/repo"
node_build=(
    docker build --pull "${source_label[@]}"
    --build-arg "BASE_IMAGE=${AUTONOMY_BASE_IMAGE:-python:3.12-slim}"
    -f "$node_src/repo/deploy/Dockerfile"
    -t "$node_ref"
)
if [[ -n "${AUTONOMY_TAILWIND_URL:-}" ]]; then
    node_build+=(--build-arg "TAILWIND_URL=$AUTONOMY_TAILWIND_URL")
fi
node_build+=("$node_src/repo")
"${node_build[@]}"

echo "==> Building Service gateway image from deploy/Dockerfile.service-gateway"
# The dashboard starts this container itself (docker compose --profile
# service-gateway from /app) whenever a Service is published, so a released
# node needs it in the lock like every other image it runs; without it the
# gateway fails with "No such image" (Windows run 5, 2026-09-27).
docker build --pull "${source_label[@]}" \
    -f "$node_src/repo/deploy/Dockerfile.service-gateway" \
    -t "$service_gateway_ref" \
    "$node_src/repo"
rm -rf "$node_src"

echo "==> Building existing session image family"
"$AGENT_BUILD" --pull --core-only
docker tag autonomy-session "$session_ref"
docker tag autonomy-session-platform "$platform_ref"
docker tag autonomy-session-dind "$dind_ref"
docker tag autonomy-host-terminal "$host_terminal_ref"

refs=("$node_ref" "$session_ref" "$platform_ref" "$dind_ref" "$host_terminal_ref" "$service_gateway_ref")
names=(
    AUTONOMY_NODE_IMAGE
    AUTONOMY_SESSION_IMAGE
    AUTONOMY_SESSION_PLATFORM_IMAGE
    AUTONOMY_SESSION_DIND_IMAGE
    AUTONOMY_HOST_TERMINAL_IMAGE
    AUTONOMY_SERVICE_GATEWAY_IMAGE
)

tmp_lock="$(mktemp "${LOCK_FILE}.tmp.XXXXXX")"
trap 'rm -f "$tmp_lock"' EXIT
{
    printf 'AUTONOMY_IMAGE_LOCK_VERSION=1\n'
    printf 'AUTONOMY_RELEASE_TAG=%s\n' "$RELEASE_TAG"
} >"$tmp_lock"

for i in "${!refs[@]}"; do
    ref="${refs[$i]}"
    repo="${ref%:*}"
    echo "==> Publishing $ref"
    docker push "$ref"

    digest_ref="$(
        docker image inspect \
            --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" |
            awk -v prefix="$repo@sha256:" 'index($0, prefix) == 1 { print; exit }'
    )"
    digest="${digest_ref#"$repo@sha256:"}"
    if [[ "$digest_ref" != "$repo@sha256:$digest" ]] \
        || [[ ! "$digest" =~ ^[0-9a-f]{64}$ ]]; then
        echo "could not resolve an exact pushed digest for $ref" >&2
        exit 1
    fi

    printf '%s=%s\n' "${names[$i]}" "$digest_ref" >>"$tmp_lock"
done

chmod 0644 "$tmp_lock"
mv "$tmp_lock" "$LOCK_FILE"
trap - EXIT
echo "==> Unsigned digest lock written to $LOCK_FILE"

# GHCR creates a first-time package PRIVATE, and an installer's anonymous
# pull then fails (Windows run 6, 2026-09-27: autonomy-service-gateway).
# Visibility has no API; the operator changes it in the package settings.
# Probe the anonymous token endpoint for every published repository so the
# release run says which packages an installer cannot reach, at push time.
if [[ "$REGISTRY" == "ghcr.io" ]] && command -v curl >/dev/null 2>&1; then
    private=()
    for ref in "${refs[@]}"; do
        repo="${ref%:*}"; repo="${repo#ghcr.io/}"
        code="$(curl -s -o /dev/null -w '%{http_code}' "https://ghcr.io/token?scope=repository:${repo}:pull" || echo 000)"
        [[ "$code" == "200" ]] || private+=("$repo ($code)")
    done
    if [[ ${#private[@]} -gt 0 ]]; then
        echo "!!! These packages cannot be pulled anonymously; an installer will refuse them:" >&2
        for entry in "${private[@]}"; do echo "    $entry" >&2; done
        echo "    Make each public: https://github.com/orgs/${NAMESPACE%%/*}/packages/container/<package>/settings -> Change visibility -> Public" >&2
    fi
fi
echo "==> An operator must now run deploy/sign-image-lock.sh"
