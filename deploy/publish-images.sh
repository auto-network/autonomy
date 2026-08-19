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

base="$REGISTRY/$NAMESPACE"
node_ref="$base/autonomy-node:$RELEASE_TAG"
session_ref="$base/autonomy-session:$RELEASE_TAG"
dashboard_ref="$base/autonomy-session-dashboard:$RELEASE_TAG"
dind_ref="$base/autonomy-session-dind:$RELEASE_TAG"

echo "==> Building node image from deploy/Dockerfile"
# The version stamp is not passed here: deploy/Dockerfile's builder stage reads
# the commit hash + date from the checkout's .git itself and drops .git before
# the final image. Building from REPO_ROOT (a git checkout) is what gives the
# published image its provenance.
node_build=(
    docker build --pull
    --build-arg "BASE_IMAGE=${AUTONOMY_BASE_IMAGE:-python:3.12-slim}"
    -f "$REPO_ROOT/deploy/Dockerfile"
    -t "$node_ref"
)
if [[ -n "${AUTONOMY_TAILWIND_URL:-}" ]]; then
    node_build+=(--build-arg "TAILWIND_URL=$AUTONOMY_TAILWIND_URL")
fi
node_build+=("$REPO_ROOT")
"${node_build[@]}"

echo "==> Building existing session image family"
"$AGENT_BUILD" --pull --core-only
docker tag autonomy-agent:latest "$session_ref"
docker tag autonomy-agent:dashboard "$dashboard_ref"
docker tag autonomy-agent:dind "$dind_ref"

refs=("$node_ref" "$session_ref" "$dashboard_ref" "$dind_ref")
names=(
    AUTONOMY_NODE_IMAGE
    AUTONOMY_SESSION_IMAGE
    AUTONOMY_SESSION_DASHBOARD_IMAGE
    AUTONOMY_SESSION_DIND_IMAGE
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
echo "==> An operator must now run deploy/sign-image-lock.sh"
