#!/usr/bin/env bash
# Launch an agent container to work on a bead.
#
# Usage: ./agents/launch.sh <bead-id> [--dry-run] [--image=autonomy-agent:TAG]
#
# Lifecycle:
# 1. Creates a git worktree on a bead-specific branch
# 2. Generates a composed prompt (primer + shared blocks + directives)
# 3. Launches container with worktree mounted read-write
# 4. Agent edits files, commits — normal Claude Code workflow
# 5. Collects results: decision.json, commit hash, experience report
# 6. Cleans up worktree (keeps branch for dispatcher to validate)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
IMAGE="autonomy-agent"

# ── Args ──────────────────────────────────────────────
BEAD_ID="${1:?Usage: launch.sh <bead-id> [--dry-run] [--image=autonomy-agent:TAG]}"
shift
DRY_RUN=false
for arg in "$@"; do
    case $arg in
        --dry-run) DRY_RUN=true ;;
        --image=*) IMAGE="${arg#*=}" ;;
    esac
done

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
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUTPUT_DIR="$REPO_ROOT/data/agent-runs/$BEAD_ID-$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"

# ── Create git worktree ──────────────────────────────
BRANCH="agent/$BEAD_ID"
WORKTREE_DIR="$REPO_ROOT/.worktrees/$BEAD_ID-$TIMESTAMP"

echo "==> Creating worktree: $BRANCH"
# Create branch from current HEAD if it doesn't exist
if ! git -C "$REPO_ROOT" rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
    git -C "$REPO_ROOT" branch "$BRANCH"
fi
mkdir -p "$(dirname "$WORKTREE_DIR")"
git -C "$REPO_ROOT" worktree add "$WORKTREE_DIR" "$BRANCH" 2>&1
echo "    Worktree: $WORKTREE_DIR"

# Save branch base BEFORE agent runs — used to detect new commits after
BRANCH_BASE=$(git -C "$WORKTREE_DIR" rev-parse HEAD)
echo "$BRANCH_BASE" > "$OUTPUT_DIR/.branch_base"

# Configure git identity in worktree
git -C "$WORKTREE_DIR" config user.name "autonomy-agent"
git -C "$WORKTREE_DIR" config user.email "agent@autonomy.local"

# ── Container name ────────────────────────────────────
CONTAINER_NAME="agent-${BEAD_ID}-$$"

if $DRY_RUN; then
    echo ""
    echo "==> DRY RUN — would launch container '$CONTAINER_NAME'"
    echo "    Image: $IMAGE"
    echo "    Bead: $BEAD_ID"
    echo "    Branch: $BRANCH"
    echo "    Worktree: $WORKTREE_DIR"
    echo "    Output: $OUTPUT_DIR"
    echo ""
    echo "--- PROMPT ---"
    echo "$PROMPT"
    # Clean up worktree on dry run
    git -C "$REPO_ROOT" worktree remove "$WORKTREE_DIR" 2>/dev/null || true
    exit 0
fi

# ── Launch ────────────────────────────────────────────
echo "==> Launching agent container: $CONTAINER_NAME"
echo "    Image: $IMAGE"
echo "    Branch: $BRANCH"
echo "    Output: $OUTPUT_DIR"

# Write prompt to temp file (avoids arg length limits)
PROMPT_FILE=$(mktemp)
echo "$PROMPT" > "$PROMPT_FILE"

# Copy credentials to a temp file readable by anyone (avoids uid mismatch on mount)
CREDS_COPY=$(mktemp)
cp "$CLAUDE_CREDS/.credentials.json" "$CREDS_COPY"
chmod 644 "$CREDS_COPY"

cleanup() {
    rm -f "$PROMPT_FILE" "$CREDS_COPY"
    # Don't remove worktree here — dispatcher decides after validation
}
trap cleanup EXIT

# Mount .git at the same absolute path so worktree's .git file reference resolves
GIT_DIR="$REPO_ROOT/.git"

# Agent session logs written here — visible to host for live tailing and graph ingestion
SESSION_DIR="$OUTPUT_DIR/sessions"
mkdir -p "$SESSION_DIR"

docker run \
    --name "$CONTAINER_NAME" \
    --rm \
    -e BD_ACTOR="agent:$CONTAINER_NAME" \
    -e BD_READONLY="${BD_READONLY:-0}" \
    -v "$CREDS_COPY:/home/agent/.claude/.credentials.json:ro" \
    -v "$GIT_DIR:$GIT_DIR" \
    -v "$REPO_ROOT/data/graph.db:/data/graph.db:ro" \
    -v "$REPO_ROOT/.beads:/data/.beads" \
    -v "$WORKTREE_DIR:/workspace/repo" \
    -v "$OUTPUT_DIR:/workspace/output" \
    -v "$SESSION_DIR:/home/agent/.claude/projects" \
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

# Get commit hash from worktree (if agent committed)
COMMIT_HASH=$(git -C "$WORKTREE_DIR" rev-parse HEAD 2>/dev/null || echo "")
BRANCH_BASE=$(cat "$OUTPUT_DIR/.branch_base" 2>/dev/null || echo "")
if [[ "$COMMIT_HASH" != "$BRANCH_BASE" ]] && [[ -n "$COMMIT_HASH" ]] && [[ -n "$BRANCH_BASE" ]]; then
    echo "    Commit: $COMMIT_HASH"
    echo "    Diff:"
    git -C "$WORKTREE_DIR" log --oneline "$BRANCH_BASE..$COMMIT_HASH" 2>/dev/null || true
    # Save commit hash for dispatcher
    echo "$COMMIT_HASH" > "$OUTPUT_DIR/.commit_hash"
else
    echo "    No new commits on $BRANCH"
fi

echo "    Output: $OUTPUT_DIR"
if [[ -f "$OUTPUT_DIR/decision.json" ]]; then
    echo "    Decision:"
    cat "$OUTPUT_DIR/decision.json"
else
    echo "    WARNING: No decision.json found"
fi

# Save worktree path for dispatcher cleanup
echo "$WORKTREE_DIR" > "$OUTPUT_DIR/.worktree_path"
echo "$BRANCH" > "$OUTPUT_DIR/.branch"

exit $EXIT_CODE
