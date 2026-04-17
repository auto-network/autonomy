#!/usr/bin/env bash
# Build the autonomy-agent container images (base + dashboard).
# Stages tool binaries into a temp dir, then builds.
#
# Usage: ./agents/build.sh [--no-cache]
#
# Always builds both autonomy-agent:latest (base) and autonomy-agent:dashboard
# (base + Python deps). Docker layer cache makes repeat builds near-instant
# when nothing has changed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$SCRIPT_DIR/.build"

# Parse flags
NO_CACHE=""
for arg in "$@"; do
    case "$arg" in
        --no-cache) NO_CACHE="--no-cache" ;;
        *) echo "Unknown flag: $arg"; exit 1 ;;
    esac
done

echo "==> Staging binaries..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/bin" "$BUILD_DIR/graph"

# bd — Go binary
BD_BIN="$HOME/go/bin/bd"
if [[ ! -f "$BD_BIN" ]]; then
    echo "ERROR: bd binary not found at $BD_BIN"
    exit 1
fi
cp "$BD_BIN" "$BUILD_DIR/bin/bd"

# dolt — required by bd for database access
DOLT_BIN="$HOME/go/bin/dolt"
if [[ ! -f "$DOLT_BIN" ]]; then
    echo "ERROR: dolt binary not found at $DOLT_BIN"
    echo "Install with: go install github.com/dolthub/dolt/go/cmd/dolt@latest"
    exit 1
fi
cp "$DOLT_BIN" "$BUILD_DIR/bin/dolt"

# claude — ELF binary
CLAUDE_BIN="$(readlink -f "$HOME/.local/bin/claude")"
if [[ ! -f "$CLAUDE_BIN" ]]; then
    echo "ERROR: claude binary not found"
    exit 1
fi
cp "$CLAUDE_BIN" "$BUILD_DIR/bin/claude"

# graph — Python module (just the tools/graph/ directory)
cp -r "$REPO_ROOT/tools/graph/"*.py "$BUILD_DIR/graph/"
# Create __init__.py for tools package
mkdir -p "$BUILD_DIR/tools_pkg"

echo "==> Building docker image..."
cd "$BUILD_DIR"

# Create build context with flat structure
mkdir -p context/bin context/graph
cp bin/bd context/bin/
cp bin/dolt context/bin/
cp bin/claude context/bin/
cp graph/*.py context/graph/

# Copy Dockerfiles and any sibling files they COPY (e.g. dind-entrypoint.sh).
cp "$SCRIPT_DIR/Dockerfile" context/
cp "$SCRIPT_DIR/dind-entrypoint.sh" context/

docker build $NO_CACHE -t autonomy-agent context/
echo "==> Done. Image: autonomy-agent:latest"
docker images autonomy-agent:latest --format "  Size: {{.Size}}"

# ── Dashboard variant (adds Python deps for API contract tests) ──
# Always built: api_session_create launches autonomy-agent:dashboard for
# terminal-container sessions. When the base is unchanged, this is a cached
# no-op; otherwise it's one thin pip-install layer on top of the base.
echo ""
echo "==> Building dashboard variant (Python deps layered on base)..."
docker build $NO_CACHE -f "$SCRIPT_DIR/Dockerfile.dashboard" -t autonomy-agent:dashboard context/
echo "==> Done. Image: autonomy-agent:dashboard"
docker images autonomy-agent:dashboard --format "  Size: {{.Size}}"

# ── DinD intermediate variant ─────────────────────────────────────
# Adds Docker CE + the shared startup-wrapper entrypoint. Project images
# that need Docker-in-Docker (enterprise, enterprise-ng) extend this.
echo ""
echo "==> Building dind variant (Docker CE + entrypoint wrapper)..."
docker build $NO_CACHE -f "$SCRIPT_DIR/Dockerfile.dind" -t autonomy-agent:dind context/
echo "==> Done. Image: autonomy-agent:dind"
docker images autonomy-agent:dind --format "  Size: {{.Size}}"

# ── Per-project images (auto-discovered) ──────────────────────────
# Each agents/projects/<name>/Dockerfile becomes autonomy-agent:<name>.
# Adding a new project image = drop a Dockerfile into agents/projects/<name>/
# and re-run this script. Build context is the agents/ directory so
# projects can reference files under agents/projects/<name>/ directly.
echo ""
echo "==> Building per-project images..."
PROJECTS_DIR="$SCRIPT_DIR/projects"
if [[ -d "$PROJECTS_DIR" ]]; then
    # Make agents/projects/ visible inside the shared build context so
    # per-project COPY lines and sibling files (startup.sh, CLAUDE.md)
    # resolve from the same context root.
    cp -r "$PROJECTS_DIR" context/projects
    shopt -s nullglob
    for dockerfile in "$PROJECTS_DIR"/*/Dockerfile; do
        project_dir="$(dirname "$dockerfile")"
        project_name="$(basename "$project_dir")"
        image_tag="autonomy-agent:$project_name"
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
