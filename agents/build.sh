#!/usr/bin/env bash
# Build the autonomy-session container images (base + dashboard).
# Assembles a small build context, then builds; nothing comes from this host.
#
# Usage: ./agents/build.sh [--no-cache] [--pull] [--core-only] [--from-settings]
#
# --from-settings skips the base family entirely and builds the
# per-workspace <org>/<workspace-id> images from their
# autonomy.workspace.provision rows (agents/image_builder.py) — the
# always-works manual path behind the dashboard's build worker.
#
# Always builds both autonomy-session (base) and autonomy-session-platform
# (base + Python deps). Docker layer cache makes repeat builds near-instant
# when nothing has changed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$SCRIPT_DIR/.build"

# Parse flags
NO_CACHE=""
PULL=""
BUILD_PROJECTS=1
for arg in "$@"; do
    case "$arg" in
        --no-cache) NO_CACHE="--no-cache" ;;
        --pull) PULL="--pull" ;;
        --core-only) BUILD_PROJECTS=0 ;;
        --from-settings)
            exec "$REPO_ROOT/.venv/bin/python" -m agents.image_builder ;;
        *) echo "Unknown flag: $arg"; exit 1 ;;
    esac
done

# The Dockerfiles fetch bd, dolt and Claude themselves, checksum-pinned, so
# nothing is staged from this host (auto-2v6ay.5). The image's Claude version
# comes from CLAUDE_VERSION, else the host's claude if one exists, else latest.
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "==> Building docker image..."
cd "$BUILD_DIR"

mkdir -p context

# Copy Dockerfiles and any sibling files they COPY (e.g. session-entrypoint.sh).
cp "$SCRIPT_DIR/Dockerfile" context/
cp "$SCRIPT_DIR/session-entrypoint.sh" context/
cp "$SCRIPT_DIR/commit_sign_shim.sh" context/
cp "$SCRIPT_DIR/grok_auth_shim.sh" context/
cp "$SCRIPT_DIR/agent_browser_shim.sh" context/

if [[ -z "${CLAUDE_VERSION:-}" ]] && command -v claude >/dev/null 2>&1; then
    CLAUDE_VERSION=$(claude --version 2>/dev/null | awk '{print $1}')
fi
docker build $NO_CACHE $PULL --build-arg CLAUDE_VERSION="${CLAUDE_VERSION:-latest}" \
    -t autonomy-session context/
echo "==> Done. Image: autonomy-session"
docker images autonomy-session --format "  Size: {{.Size}}"

# ── Dashboard variant (adds Python deps for API contract tests) ──
# Always built: api_session_create launches autonomy-session-platform for
# terminal-container sessions. When the base is unchanged, this is a cached
# no-op; otherwise it's one thin pip-install layer on top of the base.
echo ""
echo "==> Building dashboard variant (Python deps layered on base)..."
# No --pull: these variants build FROM the local autonomy-session just built.
docker build $NO_CACHE -f "$SCRIPT_DIR/Dockerfile.platform" -t autonomy-session-platform context/
echo "==> Done. Image: autonomy-session-platform"
docker images autonomy-session-platform --format "  Size: {{.Size}}"

# ── DinD intermediate variant ─────────────────────────────────────
# Adds Docker CE + the shared startup-wrapper entrypoint. Project images
# that need Docker-in-Docker (enterprise, widgets-ng) extend this.
echo ""
echo "==> Building dind variant (Docker CE + entrypoint wrapper)..."
docker build $NO_CACHE -f "$SCRIPT_DIR/Dockerfile.dind" -t autonomy-session-dind context/
echo "==> Done. Image: autonomy-session-dind"
docker images autonomy-session-dind --format "  Size: {{.Size}}"

# ── Per-project images (auto-discovered) ──────────────────────────
# Each agents/projects/<name>/Dockerfile becomes autonomy-session-<name>.
# Adding a new project image = drop a Dockerfile into agents/projects/<name>/
# and re-run this script. Build context is the agents/ directory so
# projects can reference files under agents/projects/<name>/ directly.
echo ""
echo "==> Building per-project images..."
PROJECTS_DIR="$SCRIPT_DIR/projects"
if (( BUILD_PROJECTS == 0 )); then
    echo "  (--core-only: skipping per-project images)"
elif [[ -d "$PROJECTS_DIR" ]]; then
    # Make agents/projects/ visible inside the shared build context so
    # per-project COPY lines and sibling files (startup.sh, CLAUDE.md)
    # resolve from the same context root.
    cp -r "$PROJECTS_DIR" context/projects
    shopt -s nullglob
    for dockerfile in "$PROJECTS_DIR"/*/Dockerfile; do
        project_dir="$(dirname "$dockerfile")"
        project_name="$(basename "$project_dir")"
        image_tag="session-$project_name"
        echo ""
        echo "==> Building $image_tag (from $dockerfile)..."
        docker build $NO_CACHE \
            -f "context/projects/$project_name/Dockerfile" \
            -t "$image_tag" \
            context/
        echo "==> Done. Image: $image_tag"
        docker images "$image_tag" --format "  Size: {{.Size}}"
    done
    shopt -u nullglob
else
    echo "  (no agents/projects/ directory — skipping per-project builds)"
fi

echo "==> Cleanup..."
rm -rf "$BUILD_DIR"
