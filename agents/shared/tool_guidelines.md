# Tool Guidelines

You have access to these CLI tools. Use them — they are your primary interface to the project's knowledge and work tracking.

## Workspace

Your working directory is `/workspace/repo` — a git worktree on branch `agent/<bead-id>`.
You can read, edit, create files, and commit normally. The dispatcher will collect your
commits after you exit.

## Knowledge Graph (`graph`)

```
graph search "query"              # Compact source-level search; strict first, any-term fallback on zero hits
graph search "query" --or         # Match ANY term instead of all
graph search "query" --verbose    # Show every matching excerpt (high-volume)
graph search "query" --type X     # Scope by source kind (session/note/bead/…); composes with --project / --tag / --state
graph read <src_id|tmux_name>     # Read full source content (tmux names like auto-* auto-resolve)
graph read <src_id> --max-chars N # Read with character limit
graph read <src_id> --save <path> # Export raw content to file for editing
graph context <src_id|tmux_name> <turn>  # Show turns around a search hit
graph tail <src_id|tmux_name> [N] # Last N turns of a session (default 10)
graph sources                     # List sources (--project X --type Y)
graph note "text" --tags x,y      # Drop a searchable trail marker
graph note withdraw <src_id>      # Hide from search/listings (record + links survive)
graph link <bead> <src> -r rel    # Create provenance edge
graph attention --last N          # Show recent human input
graph ui-design "title" <dir>     # Create Design Studio design from HTML files + live-watch for changes
graph dispatch approve <bead-id>  # Approve bead(s) for dispatch (accepts multiple IDs)
graph context <src_id|tmux_name> last  # Latest turns of a source
graph notes --since 1h            # Recent notes (orientation)
graph crosstalk --since 30m       # Recent CrossTalk messages
graph crosstalk send <s> "msg"    # Send CrossTalk message (-c - for stdin)
graph crosstalk broadcast "msg"   # Send to all live sessions
graph thought "text" --tags x     # Capture idea discovered during work
graph thread create "title"       # Create a thought thread
graph thread park/done/active <id> # Manage thread lifecycle
graph dispatch status <bead-id>   # Post-dispatch detail: decision, experience, session links
graph dispatch stats              # Aggregate stats: success rate, tooling, failures
graph dispatch stats --trend      # Weekly trend with direction indicators
graph dispatch stats --by-image   # Break down by container image
graph dispatch watch              # Block until next dispatch completes
graph set-label "title"           # Set session working title
graph set-topics "Line 1" "..."   # Set card status lines
graph set-role analyst            # Set session role
graph set-nag --interval 10       # Enable idle nag (--off to disable)
graph set-nag --dispatch          # Enable dispatch completion nag (--off to disable)
```

**Dispatch workflow:**
1. `graph dispatch approve <bead-id>` — release bead for dispatch
2. `graph wait <bead-id>` — block until dispatched bead completes
3. `graph dispatch status` — check overall queue at any time
4. `graph dispatch status <bead-id>` — inspect a completed run's results

**Working with settings (`graph set`):** read the Setting Primitive signpost `graph://0d3f750f-f9c` and the rubric `graph://4d88c2ad-625` (crib `graph://8cd0ded4-7d9`) first. **You MUST read the rubric when deciding the org scope or publication state of a new setting** — the wrong org or state silently misfiles it or leaks it.

## Prior-art gate

Before designing or implementing non-trivial architecture, search the source
and tests, graph notes/sessions/beads, open and closed `bd` work, and any
relevant product catalog. Search for the problem, synonyms, predecessor names,
and adjacent uses—not only the proposed component name. Read the strongest
candidates fully and verify status in code or the running system; a design
note may be aspirational.

Record a compact comparison before adding a new primitive: candidate and
evidence, current state, reusable seam, remaining gap, and the reason to
consume, extend, replace, or reject it. One empty query does not prove absence,
especially across graph org/publication boundaries. Prefer reuse, then a shared
extension; create something new only when the comparison demonstrates the
delta. The `prior-art-analysis` skill contains the complete workflow.

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
  ],
  "journal_entry": {
    "source_id": "src:abc123def456",
    "compact": "headline of the arc"
  }
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

## Definition of Done — runtime-critical work

If the bead carries the `runtime-critical` label — code exercised only when
the real system runs and stubbed by the mock/unit layers (session/container
startup + launch routing, credential/auth resolve+store+refresh, session
launcher + mount wiring, dispatch execution, merge automation, background
pollers/jobs) — a green test suite is **not** enough to be DONE.

- **`confidence: 5` requires a functional artifact.** A functional artifact is
  a REAL run's evidence — a screenshot, a log tail, a transcript — of the
  change exercised on the actual user path. A passing pytest+grep is not that:
  it proves the mocked layer, which is the layer the bug hides behind. Without
  a functional artifact your ceiling is `confidence: 2`.
- **No artifact => BLOCKED, not DONE.** This is enforced, not advisory. Three
  gates refuse an unproven close/merge (the `bd close` shim, the dispatcher's
  pre-merge check, and the dispatcher's own host-side closer). If you cannot
  produce the artifact, write a `BLOCKED` decision saying so — do not claim
  DONE and let a gate reopen the bead under you.

Record the proof as a marker line the gates recognize, in the close reason
(`-r`) or a bead note, AND as a `functional_artifacts` entry in decision.json:

```
functional-proof: <ref>
```

where `<ref>` is a graph attachment id, a `/workspace/output` path, or a run
log. Two rules the proven gate enforces — learned by it failing each one on a
real run first:

1. **The ref must be a concrete token** — six or more ref-safe characters
   matching `[A-Za-z0-9/][A-Za-z0-9/_.:-]{5,}`. `functional-proof: abcdef`,
   `functional-proof: /workspace/output/run/functional_check.log`, and
   `functional-proof: edf85f2c-5bf` all satisfy it.
2. **Literal angle-bracket examples do NOT satisfy the gate.** Quoting the
   syntax — `functional-proof: <ref>`, `functional-proof: <graph attachment
   id | output path>` — is documentation, not proof, and is rejected. This
   exclusion caught a real false-positive: the gate's own first red run sailed
   through because the bead description quoted the contract.

The exact refusal you will see if you try to close without proof (verbatim
from the fleet-proof transcript, graph attachment `edf85f2c-5bf`):

```
bd-close gate: REFUSING to close <id>
  This bead is labeled runtime-critical: closing it requires
  evidence the change ran on a real user path — a screenshot,
  tail, or transcript from an actual run, referenced as:
      functional-proof: <graph attachment id | output path>
  in the close reason (-r) or a bead note. A green test suite
  is not functional proof for a runtime change.
```

**The pipeline-driven way to produce a functional artifact:** write an
executable `functional_check.sh` into your run's output dir (next to
decision.json). Contract: `exit 0` = proven; stdout IS the evidence
transcript. The dispatcher runs it (via `smoke.py`, 120s timeout), tees stdout
to `functional_check.log`, and the pre-merge gate accepts that log as your
functional artifact when it passed. No config, no bead-side flag — the run
declares by writing the script.

## Optional: write a journal entry

If this arc revealed something an operator should follow — a substrate gap,
an unexpected pitfall, a design pattern that generalized, a non-obvious
workaround — write a journal entry BEFORE writing decision.json. Then put
the returned `source_id` in decision.json's `journal_entry.source_id` field
and a one-line outcome in `journal_entry.compact`.

Skip this if the arc was routine implementation. Most arcs are. The bar is
"operator should know about this," not "I did the work."

```bash
graph journal write "Brief headline" \
  --normal /tmp/journal-normal.md \
  --since <duration since dispatch start>
# → ✓ Journal entry saved (src:<id>)
```

The compact text in `journal_entry.compact` should be the bead's outcome in
one line, not the bead title. When the field is set, the dashboard's
Activity Feed card renders a 📔 indicator that links to your entry on the
Attention tab.

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

## Design Studio

UI beads MUST be implemented from a Design Studio design, not from prose descriptions.

Before implementing any UI bead:
1. Read the Design Studio guide: graph://225a4af7-ee5
2. Fetch the design: `curl -sk https://localhost:8080/api/design/{design_id}/full`
3. Extract the HTML from `variants[last].html`
4. Copy the HTML + CSS directly into the template — do NOT rewrite or improvise
5. Replace `window.FIXTURE` references with real component data properties
6. The design uses Tailwind `md:` classes and Alpine directives — these are production code

If the design is inaccessible, set status: BLOCKED. Do not fall back to implementing from the bead description.

## Dashboard & Design Studio

The **live** dashboard runs on the host at `https://localhost:8080` (HTTPS, self-signed cert).
From inside the container (`--network=host`), you can query its APIs to read data.
Note: this is the production dashboard — it does NOT reflect code changes you make in your
worktree. Your edits to server.py, app.js, or templates won't be visible here until after
your branch is merged and the dashboard restarts.

All curl commands MUST use `curl -sk` (silent + insecure for self-signed cert).

### Design Studio designs

If your bead references a Design Studio design, you MUST fetch and use the design
template. The design is the source of truth for the visual design. Do NOT write your
own HTML/CSS — copy the design template and wire the DAO.

If you cannot access the design template after trying the steps below, set your
decision status to BLOCKED with reason "cannot access design template." Do NOT
proceed without it.

```bash
# Step 1: List designs to find the full UUID from a partial ID
curl -sk https://localhost:8080/api/design/pending | python3 -c "
import json,sys
for e in json.load(sys.stdin):
    print(e['id'], e.get('title',''))
"

# Step 2: Fetch the design HTML using the FULL UUID
curl -sk https://localhost:8080/api/design/{FULL-UUID}/full | python3 -c "
import json,sys; d=json.load(sys.stdin)
for v in d['variants']:
    print(v['html'])
"
```

NOTE: Design files referenced as /tmp/ paths in bead specs do NOT exist in your
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

### Visual testing with mock dashboard

Start a self-daemonizing mock server that serves your worktree code with mock data:
```bash
dashboard-mock start --port 8082                    # start, prints "Ready on :8082", exits 0
dashboard-mock start --port 8082 --fixture /tmp/f.json  # custom fixture
dashboard-mock stop --port 8082                     # kill via PID file
dashboard-mock status --port 8082                   # check if running
```

The mock DAO reads fixtures.json on every request — edit the file, refresh the page, see new data.
Fixture generators are in `tools/dashboard/tests/fixtures.py`.

The server self-daemonizes (fork + setsid) so it survives after the bash tool returns — no SIGPIPE.
See `agents/shared/dashboard/agent-browser-primer.md` for visual validation patterns.

## Editing Notes

Export a note to a file, edit it, then push changes back:

```bash
graph read abc123 --save /tmp/notes/abc123.md     # export for editing
# ... use Edit tool on the file ...
graph note update abc123 -c - < /tmp/notes/abc123.md   # push changes
# ✓ Note updated — Local copy: /tmp/graph-notes/abc123.md
```

Note creation and update commands auto-save to `/tmp/graph-notes/{source_id}.md`.

## Working Style

- Work in `/workspace/repo` — edit files, commit your changes
- Write decision and reports to `/workspace/output/` — this persists after container exit
- Complete the prior-art gate before building non-trivial architecture
- Drop trail markers — `graph note` for pitfalls, insights, operational discoveries
- Commit your work — the dispatcher records your commit hash on the bead
- Stay focused — complete the assigned bead, don't scope-creep
- Report blockers — if you can't proceed, write a BLOCKED decision with details
