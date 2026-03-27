# Tool Guidelines

You have access to these CLI tools. Use them — they are your primary interface to the project's knowledge and work tracking.

## Workspace

Your working directory is `/workspace/repo` — a git worktree on branch `agent/<bead-id>`.
You can read, edit, create files, and commit normally. The dispatcher will collect your
commits after you exit.

## Knowledge Graph (`graph`)

```
graph search "query"              # Full-text search (use --or for OR mode)
graph search "query" --or         # Match ANY term instead of all
graph read <src_id>               # Read full source content
graph read <src_id> --max-chars N # Read with character limit
graph context <src_id> <turn>     # Show turns around a search hit
graph sources                     # List sources (--project X --type Y)
graph note "text" --tags x,y      # Drop a searchable trail marker
graph link <bead> <src> -r rel    # Create provenance edge
graph attention --last N          # Show recent human input
graph ui-exp "title" <dir>        # Create UI experiment from HTML files + live-watch for changes
graph dispatch approve <bead-id>  # Approve bead(s) for dispatch (accepts multiple IDs)
graph context <src_id> last       # Latest turns of a source
graph notes --since 1h            # Recent notes (orientation)
graph crosstalk --since 30m       # Recent CrossTalk messages
graph crosstalk send <s> "msg"    # Send CrossTalk message (-c - for stdin)
graph crosstalk broadcast "msg"   # Send to all live sessions
graph thought "text" --tags x     # Capture idea discovered during work
graph thread create "title"       # Create a thought thread
graph thread park/done/active <id> # Manage thread lifecycle
graph dispatch watch              # Block until next dispatch completes
graph set-label "title"           # Set session working title
graph set-topics "Line 1" "..."   # Set card status lines
graph set-role analyst            # Set session role
graph set-nag --interval 10       # Enable idle nag (--off to disable)
```

**Dispatch workflow:**
1. `graph dispatch approve <bead-id>` — release bead for dispatch
2. `graph wait <bead-id>` — block until dispatched bead completes
3. `graph dispatch status` — check overall queue at any time

## Beads (`bd`)

You are running in **read-only mode**. You cannot modify beads directly.
The dispatcher manages bead state on your behalf based on your decision file.

```
bd show <id>                      # View bead details
bd ready                          # See unblocked work
bd search "query"                 # Search beads
bd dep tree <id>                  # View dependency tree
```

## Browser (`agent-browser`)

Headless Chrome for visual validation. Available in all containers.

```
agent-browser open <url> --ignore-https-errors   # Open page
agent-browser wait --load networkidle             # Wait for load
agent-browser snapshot -i                         # DOM snapshot with refs
agent-browser screenshot --annotate               # Visual screenshot
agent-browser eval "js expression"                # Execute JS
agent-browser close                               # Close session
```

Dashboard at `https://localhost:8080` (self-signed TLS, use `--ignore-https-errors` on `open`).
See `agents/shared/dashboard/agent-browser-primer.md` for patterns and gotchas.

## Decision File

When you complete your work, write a decision file to `/workspace/output/decision.json`:

```json
{
  "status": "DONE | BLOCKED | FAILED",
  "reason": "Brief explanation",
  "artifacts": ["list", "of", "files", "produced"],
  "notes": "Anything the dispatcher should record on the bead",
  "scores": {
    "tooling": 3,
    "clarity": 4,
    "confidence": 5
  },
  "time_breakdown": {
    "research_pct": 20,
    "coding_pct": 60,
    "debugging_pct": 15,
    "tooling_workaround_pct": 5
  },
  "failure_category": "tooling|spec|timeout|code|other",
  "discovered_beads": [
    {
      "title": "New work discovered during execution",
      "description": "Details",
      "labels": ["refinement"],
      "priority": 2
    }
  ]
}
```

### Optional fields

**All fields below are optional.** Include them when you have meaningful signal — omit when unsure.

**scores** (1–5 scale, integers):

- `tooling` — infrastructure experience (bd, graph, git, container, deps). Score reflects **outcome and friction**, not fault. It does not matter whether a problem was "pre-existing" or "not your fault" — score the experience you actually had.
  - 5 = everything worked smoothly, no friction
  - 4 = minor annoyances, no workarounds needed
  - 3 = workaround needed, resolved in ≤4 turns
  - 2 = workaround needed, took 5+ turns to resolve
  - 1 = blocking tool failure (e.g. missing dependency, cannot run tests, broken CLI). Any tool failure that prevented you from completing a normal part of your workflow (testing, committing, searching) is a 1, period.

- `clarity` — could you reconstruct the bead from the diffs alone?
  - 5 = the diffs are a perfect expression of the spec — no new, missing, or conflicting content
  - 4 = diffs match the spec with minor omissions or ambiguities that did not affect the outcome
  - 3 = had to add things not in the spec, or the spec described things that did not end up being relevant
  - 2 = significant mismatch — diffs contain substantial work not described, or spec described things that could not be implemented as written
  - 1 = the spec and the diffs describe different tasks

- `confidence` — how certain are you that this fully addresses the bead and will function exactly as desired?
  - 5 = certain: tested, verified, covers all described behavior
  - 4 = high: works correctly in all cases I could verify, minor uncertainty remains
  - 3 = moderate: core functionality works, but some paths are unverified or I had to make judgment calls
  - 2 = low: I believe the approach is right but could not fully verify (e.g. tests could not run, dependency missing, environment gap)
  - 1 = uncertain: best effort given constraints, but substantial risk it does not work as intended

**time_breakdown** (approximate % of time in each phase, should roughly sum to 100):
- `research_pct` — reading docs, searching graph, understanding codebase
- `coding_pct` — writing/editing code
- `debugging_pct` — fixing tests, tracing bugs
- `tooling_workaround_pct` — fighting infrastructure (bd down, git conflicts, container issues, dep problems)

**failure_category** (only when status is BLOCKED or FAILED):
- `tooling` — bd down, git conflict, container issue, dep failure
- `spec` — task impossible or underspecified
- `timeout` — ran out of time
- `code` — tests fail, won't compile, logic errors
- `other` — anything else

## Merge Retry

If your primer includes a "MERGE RETRY" section, previous work exists on a branch. Do NOT re-implement from scratch.

1. `git cherry-pick <commit>` to apply previous work
2. Resolve conflicts if any
3. Verify the result
4. Commit normally

If cherry-pick has irreconcilable conflicts, read the diff (`git show <commit>`) and manually apply the changes.

## Provenance as Primary Source

The **Background — Original Discussions** section in your task prompt contains the conversation
where this task was conceived. These turns are your primary source material, not background color.
User messages especially may contain implementation specifics, constraints, and design decisions
that are not fully captured in the bead description. Read them carefully for any details that
would aid implementation. When the description and a user message conflict, the user message
is authoritative.

## Dashboard & Experiments

The **live** dashboard runs on the host at `https://localhost:8080` (HTTPS, self-signed cert).
From inside the container (`--network=host`), you can query its APIs to read data.
Note: this is the production dashboard — it does NOT reflect code changes you make in your
worktree. Your edits to server.py, app.js, or templates won't be visible here until after
your branch is merged and the dashboard restarts.

All curl commands MUST use `curl -sk` (silent + insecure for self-signed cert).

### Design Studio experiments

If your bead references a Design Studio experiment, you MUST fetch and use the experiment
template. The experiment is the source of truth for the visual design. Do NOT write your
own HTML/CSS — copy the experiment template and wire the DAO.

If you cannot access the experiment template after trying the steps below, set your
decision status to BLOCKED with reason "cannot access experiment template." Do NOT
proceed without it.

```bash
# Step 1: List experiments to find the full UUID from a partial ID
curl -sk https://localhost:8080/api/experiments/pending | python3 -c "
import json,sys
for e in json.load(sys.stdin):
    print(e['id'], e.get('title',''))
"

# Step 2: Fetch the experiment HTML using the FULL UUID
curl -sk https://localhost:8080/api/experiments/{FULL-UUID}/full | python3 -c "
import json,sys; d=json.load(sys.stdin)
for v in d['variants']:
    print(v['html'])
"
```

NOTE: Experiment files referenced as /tmp/ paths in bead specs do NOT exist in your
container. Always fetch via the API above.

### Other dashboard APIs

```bash
curl -sk https://localhost:8080/api/beads/list
curl -sk https://localhost:8080/api/dispatch/runs
curl -sk https://localhost:8080/api/dao/active_sessions
curl -sk https://localhost:8080/api/graph/thoughts
curl -sk https://localhost:8080/api/graph/threads
```

Use `localhost:8080`, not the Tailnet IP.

## Testing

Run tests before committing. Dashboard features have tests under `tools/dashboard/tests/`.

```bash
# Run all dashboard tests
pytest

# Run tests for the feature you changed
pytest tools/dashboard/tests/session_picker

# Run just the fast API tests (no browser)
pytest tools/dashboard/tests/session_picker/test_api.py
```

If your bead includes failing test assertions in its acceptance criteria, run them and verify they pass before writing decision.json. If tests fail, your implementation is not complete.

### Mock server for manual testing

Boot a test server with mock data — no real database, no tmux needed:
```bash
DASHBOARD_MOCK=/path/to/fixtures.json python3 -m uvicorn tools.dashboard.server:app --port 8082
```

The mock DAO reads fixtures.json on every request. Edit the file to change what the UI shows. Fixture generators are in `tools/dashboard/tests/fixtures.py`.

## Working Style

- Work in `/workspace/repo` — edit files, commit your changes
- Write decision and reports to `/workspace/output/` — this persists after container exit
- Research before building — search the graph for context before writing code
- Drop trail markers — `graph note` for pitfalls, insights, operational discoveries
- Commit your work — the dispatcher records your commit hash on the bead
- Stay focused — complete the assigned bead, don't scope-creep
- Report blockers — if you can't proceed, write a BLOCKED decision with details
