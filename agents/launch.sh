#!/usr/bin/env bash
# Launch an agent container to work on a bead.
#
# Usage: ./agents/launch.sh <bead-id> [--dry-run]
#
# What it does:
# 1. Generates a composed prompt (primer + shared blocks + directives)
# 2. Launches the autonomy-agent container with:
#    - Claude credentials (~/.claude/) mounted for subscription auth
#    - graph.db mounted read-only
#    - .beads/ mounted read-only
#    - Repo mounted read-only (agent writes to /workspace overlay)
#    - Prompt piped via --print flag + stdin
# 3. Collects decision.json and experience_report.md on exit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
IMAGE="autonomy-agent"

# ── Args ──────────────────────────────────────────────
BEAD_ID="${1:?Usage: launch.sh <bead-id> [--dry-run]}"
DRY_RUN=false
[[ "${2:-}" == "--dry-run" ]] && DRY_RUN=true

# ── Validate ──────────────────────────────────────────
CLAUDE_CREDS="${CLAUDE_CREDENTIALS_DIR:-$HOME/.claude}"
if [[ ! -f "$CLAUDE_CREDS/.credentials.json" ]]; then
    echo "ERROR: Claude credentials not found at $CLAUDE_CREDS/.credentials.json" >&2
    echo "Run 'claude setup-token' to generate a long-lived token." >&2
    exit 1
fi

if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    echo "ERROR: Docker image '$IMAGE' not found. Run agents/build.sh first." >&2
    exit 1
fi

# ── Generate prompt ───────────────────────────────────
echo "==> Generating prompt for $BEAD_ID..."
PROMPT=$("$REPO_ROOT/.venv/bin/python" -m agents.compose "$BEAD_ID")
if [[ -z "$PROMPT" ]]; then
    echo "ERROR: Empty prompt generated for $BEAD_ID" >&2
    exit 1
fi
echo "    Prompt: $(echo "$PROMPT" | wc -c) bytes"

# ── Prepare output directory ──────────────────────────
OUTPUT_DIR="$REPO_ROOT/data/agent-runs/$BEAD_ID-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUTPUT_DIR"

# ── Container name ────────────────────────────────────
CONTAINER_NAME="agent-${BEAD_ID}-$$"

if $DRY_RUN; then
    echo ""
    echo "==> DRY RUN — would launch container '$CONTAINER_NAME'"
    echo "    Image: $IMAGE"
    echo "    Bead: $BEAD_ID"
    echo "    Output: $OUTPUT_DIR"
    echo ""
    echo "--- PROMPT ---"
    echo "$PROMPT"
    exit 0
fi

# ── Launch ────────────────────────────────────────────
echo "==> Launching agent container: $CONTAINER_NAME"
echo "    Output: $OUTPUT_DIR"

# Write prompt to temp file (avoids arg length limits)
PROMPT_FILE=$(mktemp)
echo "$PROMPT" > "$PROMPT_FILE"

# Copy credentials to a temp file readable by anyone (avoids uid mismatch on mount)
CREDS_COPY=$(mktemp)
cp "$CLAUDE_CREDS/.credentials.json" "$CREDS_COPY"
chmod 644 "$CREDS_COPY"
trap 'rm -f "$PROMPT_FILE" "$CREDS_COPY"' EXIT

docker run \
    --name "$CONTAINER_NAME" \
    --rm \
    -e BD_ACTOR="agent:$CONTAINER_NAME" \
    -v "$CREDS_COPY:/home/agent/.claude/.credentials.json:ro" \
    -v "$REPO_ROOT/data/graph.db:/data/graph.db:ro" \
    -v "$REPO_ROOT/.beads:/data/.beads:ro" \
    -v "$REPO_ROOT:/repo:ro" \
    -v "$OUTPUT_DIR:/workspace/output" \
    -v "$PROMPT_FILE:/tmp/prompt.md:ro" \
    --entrypoint claude \
    "$IMAGE" \
    --dangerously-skip-permissions \
    --print \
    "$(cat "$PROMPT_FILE")"

EXIT_CODE=$?

# ── Collect results ───────────────────────────────────
echo ""
echo "==> Agent exited with code: $EXIT_CODE"
echo "    Output directory: $OUTPUT_DIR"

if [[ -f "$OUTPUT_DIR/decision.json" ]]; then
    echo "    Decision:"
    cat "$OUTPUT_DIR/decision.json"
else
    echo "    WARNING: No decision.json found"
fi

exit $EXIT_CODE
