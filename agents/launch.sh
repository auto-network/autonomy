#!/usr/bin/env bash
# Launch an agent container to work on a bead.
#
# Usage: ./agents/launch.sh <bead-id> [--dry-run] [--image=autonomy-session-TAG] [--detach] [--harness=claude|codex|grok] [--org=SLUG] [--workspace-id=ID] [--model=NAME]
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
# Python for the launcher modules: the host venv when it is a real interpreter,
# otherwise the image's python3 (inside the Compose node the bind-mounted
# .venv/bin/python is a dangling symlink into the host filesystem, and the
# repo is already on PYTHONPATH there).
PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"
IMAGE="autonomy-session"

# ── Args ──────────────────────────────────────────────
BEAD_ID="${1:?Usage: launch.sh <bead-id> [--dry-run] [--image=autonomy-session-TAG] [--detach] [--harness=claude|codex|grok] [--org=SLUG] [--graph-tags=a,b,c]}"
shift
DRY_RUN=false
DETACH=false
HARNESS="claude"
ORG=""
GRAPH_PROJECT=""
GRAPH_TAGS=""
WORKSPACE_ID=""
MODEL=""
for arg in "$@"; do
    case $arg in
        --dry-run) DRY_RUN=true ;;
        --detach) DETACH=true ;;
        --image=*) IMAGE="${arg#*=}" ;;
        --harness=*) HARNESS="${arg#*=}" ;;
        --org=*) ORG="${arg#*=}" ;;
        --graph-project=*) GRAPH_PROJECT="${arg#*=}" ;;  # deprecated alias, use --org
        --graph-tags=*) GRAPH_TAGS="${arg#*=}" ;;
        --workspace-id=*) WORKSPACE_ID="${arg#*=}" ;;
        --model=*) MODEL="${arg#*=}" ;;
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
    # No env var and no ~/.claude file: the Python launcher below resolves
    # the credential from the Settings substrate (dashboard.claude.setup_tokens,
    # session_launcher._resolve_credentials_via_substrate) and fails with its
    # own error if none exists. The Compose dispatcher container has neither
    # an env token nor a ~/.claude, so exiting here blocked every dispatch
    # launch since the cutover (observed 2026-09-07: 0 launches, every cycle
    # "No Claude credentials found") while the substrate held two accounts.
    AUTH_MODE="substrate"
fi
echo "    Auth: $AUTH_MODE"

if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    echo "ERROR: Docker image '$IMAGE' not found. Run agents/build.sh first." >&2
    exit 1
fi

# Return 0 (wedged) if a worktree is mid-git-op or holds a stale lock.
# A worktree PRESERVED from an interrupted run (auto-cpxdp) is reused verbatim
# on the next dispatch; if that tree is halfway through a rebase/merge/
# cherry-pick or carries a stale index.lock, reusing it would re-wedge the
# retry. Callers abandon reuse and start fresh when this returns 0. A worktree
# whose git dir can't be resolved is treated as wedged (start fresh, safe).
worktree_git_wedged() {
    local wt="$1"
    local gitdir
    gitdir=$(git -C "$wt" rev-parse --git-dir 2>/dev/null) || return 0
    case "$gitdir" in
        /*) ;;
        *) gitdir="$wt/$gitdir" ;;
    esac
    local marker
    for marker in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD \
                  REVERT_HEAD BISECT_LOG index.lock; do
        if [ -e "$gitdir/$marker" ]; then
            echo "    Broken-git state in $wt: found $marker"
            return 0
        fi
    done
    return 1
}

# ── Generate prompt ───────────────────────────────────
echo "==> Generating prompt for $BEAD_ID..."
PROMPT=$("$PYTHON" -m agents.compose "$BEAD_ID")
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
# Prune stale worktree records (directory deleted but git reference remains).
#
# Safe by construction (bead auto-jbz67): `git worktree prune` only removes a
# record whose *working tree is missing*. A live worktree's directory exists,
# so its record is never pruned, and these dispatcher worktrees already carry
# session-unique basenames (`.worktrees/<bead>-<timestamp>`) — git never has to
# disambiguate identical basenames with a numeric suffix here, so a prune can
# never free a suffix that another live `.git` file still references. `-v`
# logs each pruned record into the launch log for an audit trail.
git -C "$REPO_ROOT" worktree prune -v

# Detect existing worktree for this branch and REUSE it. Supports the TDD
# handoff pattern: host pre-commits failing tests to agent/BEAD_ID at
# .worktrees/BEAD_ID, then the dispatcher boots directly into that worktree
# with the tests already present — no merge, no Step 0 ceremony. If no
# worktree exists (fresh dispatch), create branch-from-master + fresh
# timestamped worktree as before.
existing_wt=$(git -C "$REPO_ROOT" worktree list --porcelain \
    | awk -v br="refs/heads/$BRANCH" '/^worktree / {wt=$2} $0=="branch "br {print wt}')
WORKTREE_REUSED=false
# Broken-git guard (auto-cpxdp): if the preserved worktree is mid-git-op or
# holds a stale index.lock, abandon reuse and start fresh — reusing a wedged
# tree would re-wedge the retry.
if [ -n "$existing_wt" ] && worktree_git_wedged "$existing_wt"; then
    echo "    Preserved worktree $existing_wt is wedged — abandoning reuse, starting fresh"
    git -C "$REPO_ROOT" worktree remove "$existing_wt" --force 2>/dev/null || true
    existing_wt=""
fi
if [ -n "$existing_wt" ]; then
    echo "    Reusing existing worktree: $existing_wt"
    WORKTREE_DIR="$existing_wt"
    WORKTREE_REUSED=true
else
    if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null 2>&1; then
        git -C "$REPO_ROOT" branch "$BRANCH"
    fi
    mkdir -p "$(dirname "$WORKTREE_DIR")"
    git -C "$REPO_ROOT" worktree add "$WORKTREE_DIR" "$BRANCH"
fi
echo "    Worktree: $WORKTREE_DIR"

# ── Preserved-worktree handoff block (auto-cpxdp) ─────────────────────
# When we REUSE an existing worktree it may be a tree PRESERVED from a
# previous interrupted dispatch of this bead (timed out / exited without a
# decision). If it carries uncommitted changes, inject a block into the
# composed prompt telling the retry agent to BUILD ON the preserved work
# instead of restarting. $PROMPT was composed at :85 (before worktree
# resolution); the prompt FILE is written below (:166+), so appending here
# lands the block in the file the agent actually reads.
if $WORKTREE_REUSED; then
    HANDOFF_CHANGES=$(git -C "$WORKTREE_DIR" status --porcelain 2>/dev/null || true)
    if [ -n "$HANDOFF_CHANGES" ]; then
        HANDOFF_COUNT=$(printf '%s\n' "$HANDOFF_CHANGES" | grep -c .)
        PROMPT="$PROMPT

---

# ⚠️ PRESERVED WORKTREE — build on the work already here

This worktree was **PRESERVED from a previous interrupted run of this bead**
(it timed out or exited without writing a decision). It was deliberately NOT
discarded: it contains **uncommitted changes** from that run. Build on them —
do **not** restart from scratch, and do not \`git reset\`/\`git checkout\` them
away.

Uncommitted changes present now (\`git status --porcelain\`):

\`\`\`
$HANDOFF_CHANGES
\`\`\`

Run \`git status\` and \`git diff\` first to review what the previous run left,
then continue that work and commit it."
        echo "    Preserved-worktree handoff: injected $HANDOFF_COUNT changed path(s) into prompt"
    fi
fi

# Save branch base BEFORE agent runs — used to detect new commits after
BRANCH_BASE=$(git -C "$WORKTREE_DIR" rev-parse HEAD)
echo "$BRANCH_BASE" > "$OUTPUT_DIR/.branch_base"

# Git identity is inherited from the repo config (one source of truth) — do not
# hardcode it here. Change it via `git config user.{name,email}` on the host +
# bare repos if it ever needs to move.

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
    # Clean up worktree on dry run — but only if we created it. Reused
    # worktrees (TDD handoff pattern) belong to the user and must survive.
    if ! $WORKTREE_REUSED; then
        git -C "$REPO_ROOT" worktree remove "$WORKTREE_DIR" 2>/dev/null || true
    fi
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
if [[ -n "$ORG" ]]; then
    SCOPE_ARGS+=("--org" "$ORG")
elif [[ -n "$GRAPH_PROJECT" ]]; then
    SCOPE_ARGS+=("--graph-project" "$GRAPH_PROJECT")
fi
if [[ -n "$GRAPH_TAGS" ]]; then
    SCOPE_ARGS+=("--graph-tags" "$GRAPH_TAGS")
fi
if [[ -n "$WORKSPACE_ID" ]]; then
    SCOPE_ARGS+=("--workspace-id" "$WORKSPACE_ID")
fi

# Forward the model override only when set. When empty, launch_session_cli's
# own workspace-then-default chain resolves the model exactly as before.
MODEL_ARGS=()
if [[ -n "$MODEL" ]]; then
    MODEL_ARGS+=("--model" "$MODEL")
fi

if $DETACH; then
    # ── Detached mode: delegate to Python launch_session_cli ──
    LAUNCH_OUTPUT=$("$PYTHON" -m agents.launch_session_cli \
        --session-type dispatch \
        --name "$CONTAINER_NAME" \
        --prompt-file "$PROMPT_FILE" \
        --bead-id "$BEAD_ID" \
        --worktree "$WORKTREE_DIR" \
        --git-dir "$GIT_DIR" \
        --output-dir "$OUTPUT_DIR" \
        --image "$IMAGE" \
        --harness "$HARNESS" \
        ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"} \
        ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} \
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
"$PYTHON" -m agents.launch_session_cli \
    --session-type dispatch \
    --name "$CONTAINER_NAME" \
    --prompt-file "$PROMPT_FILE" \
    --bead-id "$BEAD_ID" \
    --worktree "$WORKTREE_DIR" \
    --git-dir "$GIT_DIR" \
    --output-dir "$OUTPUT_DIR" \
    --image "$IMAGE" \
    --harness "$HARNESS" \
    ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"} \
    ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"}

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
