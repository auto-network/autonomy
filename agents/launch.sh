#!/usr/bin/env bash
# Launch an agent container to work on a bead.
#
# Usage: ./agents/launch.sh <bead-id> [--dry-run] [--image=autonomy-agent:TAG] [--detach]
#
# Lifecycle (foreground mode — default):
# 1. Creates a git worktree on a bead-specific branch
# 2. Generates a composed prompt (primer + shared blocks + directives)
# 3. Launches container with worktree mounted read-write
# 4. Agent edits files, commits — normal Claude Code workflow
# 5. Collects results: decision.json, commit hash, experience report
# 6. Cleans up worktree (keeps branch for dispatcher to validate)
#
# With --detach:
# Steps 1-2 as above, then launches container in background (docker run -d).
# Writes container metadata to output dir for the dispatcher to poll and collect.
# The dispatcher calls poll_container() / collect_results() separately.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
IMAGE="autonomy-agent"

# ── Args ──────────────────────────────────────────────
BEAD_ID="${1:?Usage: launch.sh <bead-id> [--dry-run] [--image=autonomy-agent:TAG] [--detach] [--graph-project=NAME] [--graph-tags=a,b,c]}"
shift
DRY_RUN=false
DETACH=false
GRAPH_PROJECT=""
GRAPH_TAGS=""
for arg in "$@"; do
    case $arg in
        --dry-run) DRY_RUN=true ;;
        --detach) DETACH=true ;;
        --image=*) IMAGE="${arg#*=}" ;;
        --graph-project=*) GRAPH_PROJECT="${arg#*=}" ;;
        --graph-tags=*) GRAPH_TAGS="${arg#*=}" ;;
    esac
done

# ── Validate credentials ─────────────────────────────
# Prefer long-lived setup token (env var) over OAuth credentials file.
CLAUDE_CREDS="${CLAUDE_CREDENTIALS_DIR:-$HOME/.claude}"
SETUP_TOKEN_FILE="$CLAUDE_CREDS/.setup-token"
if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    AUTH_MODE="token"
elif [[ -f "$SETUP_TOKEN_FILE" ]]; then
    CLAUDE_CODE_OAUTH_TOKEN="$(cat "$SETUP_TOKEN_FILE")"
    AUTH_MODE="token"
elif [[ -f "$CLAUDE_CREDS/.credentials.json" ]]; then
    AUTH_MODE="creds_file"
else
    echo "ERROR: No Claude credentials found." >&2
    echo "Either: set CLAUDE_CODE_OAUTH_TOKEN, save token to $SETUP_TOKEN_FILE," >&2
    echo "        or run 'claude login' to create $CLAUDE_CREDS/.credentials.json" >&2
    exit 1
fi
echo "    Auth: $AUTH_MODE"

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
# Prune stale worktree records (directory deleted but git reference remains)
git -C "$REPO_ROOT" worktree prune
# Clean up any existing worktree for this branch (stale from prior/failed runs)
existing=$(git -C "$REPO_ROOT" worktree list --porcelain | grep -B1 "branch refs/heads/$BRANCH" | grep "^worktree " | awk '{print $2}' || true)
if [ -n "$existing" ]; then
    echo "    Removing stale worktree: $existing"
    git -C "$REPO_ROOT" worktree remove "$existing" --force 2>/dev/null || true
fi
# Create branch from current HEAD only if it does not already exist.
# Preserves any pre-existing branch content — TDD-style pre-committed
# failing tests on agent/BEAD_ID (e.g. auto-bv343), or commits from a
# prior dispatch that failed after committing. The previous implementation
# did `git branch -D` + `git branch`, which silently orphaned any commit
# on the branch that was not already on master.
if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null 2>&1; then
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

# ── Write prompt file ─────────────────────────────────
PROMPT_FILE="$OUTPUT_DIR/.prompt.md"
echo "$PROMPT" > "$PROMPT_FILE"

# Mount .git at the same absolute path so worktree's .git file reference resolves
GIT_DIR="$REPO_ROOT/.git"

# ── Launch ────────────────────────────────────────────
echo "==> Launching agent container: $CONTAINER_NAME"
echo "    Image: $IMAGE"
echo "    Branch: $BRANCH"
echo "    Output: $OUTPUT_DIR"

SCOPE_ARGS=()
if [[ -n "$GRAPH_PROJECT" ]]; then
    SCOPE_ARGS+=("--graph-project" "$GRAPH_PROJECT")
fi
if [[ -n "$GRAPH_TAGS" ]]; then
    SCOPE_ARGS+=("--graph-tags" "$GRAPH_TAGS")
fi

if $DETACH; then
    # ── Detached mode: delegate to Python launch_session_cli ──
    LAUNCH_OUTPUT=$("$REPO_ROOT/.venv/bin/python" -m agents.launch_session_cli \
        --session-type dispatch \
        --name "$CONTAINER_NAME" \
        --prompt-file "$PROMPT_FILE" \
        --bead-id "$BEAD_ID" \
        --worktree "$WORKTREE_DIR" \
        --git-dir "$GIT_DIR" \
        --output-dir "$OUTPUT_DIR" \
        --image "$IMAGE" \
        ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"} \
        --detach)

    if [[ $? -ne 0 ]]; then
        echo "ERROR: launch_session_cli failed" >&2
        exit 1
    fi

    CONTAINER_ID=$(echo "$LAUNCH_OUTPUT" | grep "^CONTAINER_ID=" | cut -d= -f2-)

    # Write metadata for dispatcher polling/collection
    echo "$CONTAINER_ID" > "$OUTPUT_DIR/.container_id"
    echo "$CONTAINER_NAME" > "$OUTPUT_DIR/.container_name"
    echo "$WORKTREE_DIR" > "$OUTPUT_DIR/.worktree_path"
    echo "$BRANCH" > "$OUTPUT_DIR/.branch"

    # Print structured key=value output for dispatcher to parse
    echo "CONTAINER_ID=$CONTAINER_ID"
    echo "CONTAINER_NAME=$CONTAINER_NAME"
    echo "OUTPUT_DIR=$OUTPUT_DIR"
    echo "WORKTREE_DIR=$WORKTREE_DIR"
    echo "BRANCH=$BRANCH"
    echo "BRANCH_BASE=$BRANCH_BASE"
    exit 0
fi

# ── Foreground mode: delegate to Python launch_session_cli ───
"$REPO_ROOT/.venv/bin/python" -m agents.launch_session_cli \
    --session-type dispatch \
    --name "$CONTAINER_NAME" \
    --prompt-file "$PROMPT_FILE" \
    --bead-id "$BEAD_ID" \
    --worktree "$WORKTREE_DIR" \
    --git-dir "$GIT_DIR" \
    --output-dir "$OUTPUT_DIR" \
    --image "$IMAGE" \
    ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"}

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
