#!/usr/bin/env bash
# Build the autonomy-agent container image.
# Stages tool binaries into a temp dir, then builds.
#
# Usage: ./agents/build.sh [--no-cache] [--dashboard]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$SCRIPT_DIR/.build"

# Parse flags
NO_CACHE=""
DASHBOARD=false
for arg in "$@"; do
    case "$arg" in
        --no-cache)  NO_CACHE="--no-cache" ;;
        --dashboard) DASHBOARD=true ;;
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

# Copy Dockerfile
cp "$SCRIPT_DIR/Dockerfile" context/

docker build $NO_CACHE -t autonomy-agent context/

echo "==> Done. Image: autonomy-agent"
docker images autonomy-agent:latest --format "  Size: {{.Size}}"

# ── Dashboard variant (adds Python deps for API contract tests) ──
if [[ "$DASHBOARD" == true ]]; then
    echo ""
    echo "==> Building dashboard variant (Python deps only — Chrome/agent-browser in base)..."
    docker build $NO_CACHE -f "$SCRIPT_DIR/Dockerfile.dashboard" -t autonomy-agent:dashboard context/
    echo "==> Done. Image: autonomy-agent:dashboard"
    docker images autonomy-agent:dashboard --format "  Size: {{.Size}}"
fi

echo "==> Cleanup..."
rm -rf "$BUILD_DIR"
