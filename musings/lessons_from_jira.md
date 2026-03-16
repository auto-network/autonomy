# Lessons from Jira: Agent Orchestration Architecture

**Source:** Extracted from the `jira` project knowledge graph.
**Purpose:** Inform the "lessons from jira" section of Focus requirements.

---

## 1. How Agent Jobs Are Queued and Dispatched

### Primary Entry Points

There are three ways to dispatch an agent job:

**CLI (Direct):**
```bash
./tools/agent/run_agent.sh <agent_name> auto --param=value
```
The `run_agent.sh` script is the universal runner. It handles container lifecycle, context preparation, execution, output collection, and cleanup. The caller specifies the agent name and any parameters as `--key=value` flags. These are parsed into a dict and passed to the agent's `prepare_context()` function.

**sprint.sh (Sprint Orchestration):**
```bash
./tools/sprint/sprint.sh next
./tools/sprint/sprint.sh start --name="..." --spec=<path> --repo=<org/repo>
```
`sprint.sh` wraps `run_agent.sh` with state tracking in `SPRINTS.db` (SQLite). It determines which agent to run next based on the current sprint state, records the invocation, runs the agent, validates outputs, and updates the DB. The recommended orchestration pattern from Claude Code is:
```
1. Bash(./tools/sprint/sprint.sh next, run_in_background=true) → task_id
2. TaskOutput(task_id, block=true, timeout=600000)
3. Check outputs, if OK → goto 1
```

**Slack Bot (User-Facing):**
A Python service using Slack's Socket Mode (no webhooks, no public ports — outbound WebSocket only). When a user sends a message, a Claude Haiku call (~$0.0003, ~200-500ms) parses the natural language intent into a structured `{agent_name, parameters}` dict. If confidence ≥ 0.7, the job is submitted to the `AgentPoolManager`. Tasks are discovered dynamically from the `agents/*/slack.md` (and `prompt.md` frontmatter) files — no hardcoded task list.

### Queue Mechanics (Slack Bot)

The bot maintains an in-memory `deque` as the job queue and a pool of pre-warmed containers. There is no Redis or external message broker. State lives in process memory, keyed by `job_id`. When all agents are busy, jobs queue; when an agent finishes and recycles, it dequeues the next job.

---

## 2. How Environments Are Set Up

### Docker-in-Docker (DinD) Isolation

Each agent run gets a fully isolated virtual Docker host. The stack for one agent consists of two containers launched via docker compose with a per-agent `AGENT_ID`:

- **`dind-<ID>`** — Docker-in-Docker daemon (`docker:27-dind`, privileged, `--storage-driver=overlay2`). Has its own named volume (`dind-<ID>-data`) for `/var/lib/docker`. No TLS (private network only). Exposes Docker API on `tcp://dind:2375`.
- **`agent-<ID>`** — Claude CLI + tools container. `DOCKER_HOST=tcp://dind:2375` so all Docker/compose commands go to the private daemon without modification.

Each stack has its own network (`agent-<ID>-net`), so port conflicts are impossible between agents. Existing Makefiles and Compose files work unchanged — no rewrites needed.

**Startup sequence:**
1. `start_agent.sh` clones a "golden volume" (`anchore-dind-golden`) into a fresh `agent-<ID>_dind-data` volume (~20-30s). The golden volume has pre-loaded Docker images to avoid re-pulling.
2. `docker compose up -d` starts both containers.
3. `agent_startup_enterprise.sh` runs inside the container: copies the workspace to a shared volume, installs Poetry dependencies, loads images into the dind, starts ~13 services via `make up mode=slim`.
4. Total cold-start overhead: ~3 minutes on first boot.

**Cleanup:** A `trap cleanup EXIT` in the main orchestrator script always fires `kill_agent.sh`, which does `docker kill`, `docker rm`, `docker volume rm` on all agent-specific resources (~7GB freed per agent, ~15 seconds).

### OverlayFS Optimization

The workspace copy + Poetry install (~20s) was eliminated using OverlayFS union mounts:

```bash
mount -t overlay overlay \
  -o lowerdir=/workspace/enterprise,\
     upperdir=/workspace-shared/upper,\
     workdir=/workspace-shared/work \
  /workspace-shared/enterprise
```

- **Lower layer:** Read-only image with Poetry `.venv` baked in at build time.
- **Upper layer:** Per-agent writable copy-on-write layer (only changed files stored).
- **Merged view:** What the agent and dind see at `/workspace-shared/enterprise`.

This requires `privileged: true` (or `CAP_SYS_ADMIN`) on the agent container. Result: cold-start from ~39s down to ~20s (49% reduction). Disk usage is minimal since only changes land in the upper layer.

### Multi-Repo Support

The agent image can bake in multiple repos (`anchore/enterprise`, `anchore/anchorectl`, `anchore/e2e-testing`) at build time. An `AGENT_REPO` environment variable selects which one is active at runtime. OverlayFS mounts the selected repo to `/workspace-shared/`.

### Knowledge Base Overlay

CLAUDE.md and `**/ABOUT.md` files are "baked into" the workspace at image build time from `kb/{org}/{repo}/`. These files are read-only context that helps the agent understand the codebase without needing to explore it from scratch. They appear in `git status` (a known/accepted wart).

### Sprint Overlay (Shared Mutable State)

For sprint agents, a second volume mount exposes shared mutable state between agent invocations:

- **Host:** `repo_overlay/{org}/{repo}/` — Contains `plan/` (architect's work), `status/active/` (current run), `status/consolidated/` (completed run history).
- **Container:** `/agent_overlay/` — Live volume mount; writes are immediately visible on host and to subsequent agents.

This is how the Planner, Runner, Reviewer, and Consolidator share state across separate container runs without any message passing.

---

## 3. How Agent Runs Are Monitored

### Per-Run Log Directory

Each invocation writes artifacts to a timestamped directory on the host:
```
runs/<agent_name>/YYYYMMDD_HHMMSS/
├── context.txt           # Full context piped to Claude
├── start_agent.log       # Container startup log
├── agent_session.log     # Raw stream-json output from Claude CLI
├── session_summary.md    # Human-readable markdown (converted by format_agent_session.py)
├── run_metadata.json     # Agent name, args, timestamp (for retry)
└── agent_output/         # Extracted from container via docker cp
    ├── decision.txt
    ├── jira_comment.md / answer.md / session_state.txt
    └── stashed_changes.patch (if code was written)
```

### Web Viewer

A Flask application (`tools/web/viewer.py`) provides a web UI backed by `SPRINTS.db`. It shows:
- Sprint list with status and timestamps
- Sprint detail: full invocation timeline grouped by run, per-invocation artifact chips (decision, run plan, analysis, commit notice), diff stats (files changed, lines added/removed)
- **Live tail panel:** Polls `/api/sprint/{id}/live` every 5s. Parses the agent's running `agent_session.log` (stream-json) and renders the latest assistant turn as markdown in a pinned, collapsible footer. Includes a sticky status bar showing agent name, run number, live headline from the log, and hh:mm:ss elapsed timer.
- `TEMPLATES_AUTO_RELOAD = True` so template changes apply without restart. Python code changes require a manual server restart (this bit Jeremy at least once).

### SPRINTS.db

SQLite database tracking all invocations:

```sql
-- One row per sprint
CREATE TABLE sprints (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    repo TEXT,
    branch TEXT,
    status TEXT,   -- active, completed
    archive_dir TEXT,
    created_at TEXT,
    completed_at TEXT
);

-- One row per agent invocation
CREATE TABLE invocations (
    id INTEGER PRIMARY KEY,
    sprint_id INTEGER,
    agent_name TEXT,
    run_number INTEGER,
    iteration INTEGER,
    status TEXT,       -- running, success, failure, timeout, skipped
    decision TEXT,     -- PROCEED, COMMIT, CONTINUE, CONSOLIDATE, etc.
    run_dir TEXT,      -- path to run artifacts on host
    files_changed INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    started_at TEXT,
    completed_at TEXT,
    duration_seconds REAL,
    cost_usd REAL
);
```

**Key constraint:** Never use raw `sqlite3` against SPRINTS.db directly. Always use `sprint.sh` subcommands (`status`, `next`, `retry`, `rollback`, `run <agent>`). Raw SQL can corrupt the state machine.

### Validation

`tools/sprint/inspect_run.sh` validates run outputs after each agent: checks expected files exist, decision values are valid, patch is non-empty if expected. `sprint.sh` calls this automatically and gates the DB state transition on it passing.

---

## 4. How Results Are Collected

### The docker cp Pattern

The agent writes all outputs to `/agent_output/` inside the container. The host orchestrator copies everything out after execution:

```bash
docker cp "agent-${AGENT_ID}:/workspace-shared/enterprise/.agent_output" \
    "${RUN_DIR}/agent_output"
```

There are no shared mounts during execution (security boundary between host and container).

### What Gets Extracted

| File | Created by | When |
|------|-----------|------|
| `decision.txt` | Claude (the agent) | Always |
| `jira_comment.md` | Claude | JIRA-facing agents |
| `answer.md` | Claude | Research agent |
| `session_state.txt` | Claude | Sprint runner |
| `commit_message.txt` / `commit_notice.md` | Claude | When code is written |
| `stashed_changes.patch` | `agent_runner.sh` post-processing | When decision is PROCEED/COMMIT |

### Patch Creation (Inside Container)

Code changes are captured as a binary git patch **inside the container** (where the git working tree lives), not on the host:

```bash
git add tests/                            # or whatever dirs changed
git diff --cached --binary > .agent_output/stashed_changes.patch
git reset tests/                          # unstage (don't leave staged)
```

The host then processes the patch file from `agent_output/`.

### Post-Run Processing on Host

For JIRA-facing agents (`process_agent_output_containerized.sh`):
- **PROCEED:** Creates a git blob from the patch (`git hash-object -w`), creates a ref (`refs/ai/diff/{ISSUE_KEY}`), pushes to origin. Posts JIRA comment. Archives comment to `issues_completed/{ISSUE_KEY}/`.
- **NEEDS_INFO / NOT_SUITABLE:** Posts JIRA comment only.

For sprint agents (`process_results.py` in agent_common):
- **COMMIT:** Calls `worktree_commit_and_push()` — creates a temporary git worktree, applies the patch there, commits with the agent's message, pushes to origin. Uses worktrees to avoid touching the agent's own checkout.
- **REMEDIATE:** Feeds the `remediation_plan.md` back to the runner.

---

## 5. The Orchestration State Machine (SPRINTS.db)

### The Five Sprint Agents

| Agent | Role | Key Decision Output |
|-------|------|---------------------|
| **Architect** | Transforms spec into `SPRINT_PLAN.md` + phase docs (one-time) | N/A |
| **Planner** | Scopes next "run" (1-3 tasks) | `NEXT_RUN` or `SPRINT_COMPLETE` |
| **Runner** | Executes development work (code, tests) | `session_state.txt`: `IN_PROGRESS`, `COMPLETED`, `BLOCKED` |
| **Reviewer** | Analyzes runner sessions, extracts learnings, decides iteration | `CONTINUE` or `CONSOLIDATE` |
| **Consolidator** | Archives run, decides commit readiness | `COMMIT` or `REMEDIATE` |

### State Transition Logic

```
START:
  Planner → decision: NEXT_RUN → creates run-plan.md
                      SPRINT_COMPLETE → archive + done

After Planner (NEXT_RUN):
  Runner (iteration 1)

After Runner:
  Reviewer (--previous_run=<runner_run_dir>)
    → CONTINUE: Runner (--previous_run=<reviewer_run_dir>) [max 6 iterations]
    → CONSOLIDATE: Consolidator (--previous_run=<reviewer_run_dir>)

After Consolidator:
    → COMMIT: apply patch, git commit/push, back to Planner
    → REMEDIATE: Runner (with remediation_plan.md as context)
```

`sprint.sh next` implements this entire `determine_next_agent()` logic by reading the last invocation's agent type and decision, then dispatching accordingly.

### Reviewer as Learning System

The Reviewer is not a pass/fail gate. It writes `session-NNN.md` files to `status/active/` which the Runner reads in subsequent iterations. These reports contain what was accomplished, where the Runner struggled, command failures, and **strategies for the next iteration**. Session reports accumulate across a run; the Reviewer cross-references the Runner's Runtime Verification Report against the actual session log. Unsubstantiated claims of success → CONTINUE (re-run).

### Run Isolation

Work is organized into "runs" (1-3 related tasks). Each run has a scope (`run-plan.md`), iterates up to 6 times, and terminates at a commit. This provides natural commit granularity and prevents context overload across a very long sprint.

### Rollback

`sprint.sh rollback` walks backward from the latest invocation, marks runner/reviewer/consolidator as `skipped`, copies files from `consolidated/<timestamp>/` back to `status/active/`, and resets the state machine to re-run from the runner. If the consolidator had already committed, the operator must manually `git reset --hard HEAD~1 && git push --force-with-lease`.

---

## 6. What's Too Heavyweight / Purpose-Built to Generalize

These are the elements of the jira system that are highly specific to its context and would need abstraction for a general-purpose agent framework:

### Hardcoded to Anchore's Stack

- **Enterprise service boot** (`make up mode=slim`, 13 services) is baked into `agent_startup_enterprise.sh` — not a general "start the app" abstraction.
- **JIRA API integration** (`write_jira_comment.sh`, `read_ticket_clean.sh`, ADF-to-markdown conversion) is entirely anchore/JIRA-specific.
- **git refs at `refs/ai/diff/`** as a patch transport mechanism is clever but Anchore-specific; a general system might use artifact storage, S3, or just the filesystem.
- **The "golden volume"** (pre-loaded Docker images cloned via `tar cf - | tar xf -`) is a startup optimization specific to the Enterprise image set.
- **`kb/{org}/{repo}/ABOUT.md`** files are Anchore's knowledge base format. A general system needs a more configurable mechanism for injecting repo context.

### Shell Script Orchestration

The entire control flow is bash: `run_containerized_agent.sh`, `start_agent.sh`, `agent_startup_enterprise.sh`, `agent_runner.sh`, `process_agent_output_containerized.sh`, `kill_agent.sh`. This is 6 scripts that must be orchestrated in the right order. The handoffs (stdin pipeline, docker cp, trap-based cleanup) work but are fragile and hard to extend. In particular:

- **The stdin pipeline** (`cat context | docker exec -i`) is clever but means no mid-run monitoring and no ability to inject context after start.
- **Hardcoded paths** in scripts (`/home/jeremy/jira/...`) were a recurring bug and required a refactor; any general system must derive paths from configuration.
- **No timeout/watchdog for the shell scripts** — if `docker exec` hangs, the host script hangs indefinitely (the sprint system had a separate `wait_for_agent.sh` / watchdog, but the older per-issue scripts did not).

### In-Memory Slack State

The Slack bot's state (`deque`, `dict`, `threading.Lock`) lives entirely in process memory. A crash loses all queued and in-flight jobs with no recovery path. For production use, this needs a durable queue (Redis, Postgres, etc.) and a way to reconcile in-flight container state after restart.

### Sprint State Machine Fragility

- The state machine lives in `sprint.sh`'s `determine_next_agent()` bash function plus raw SQL in SPRINTS.db. There's no formal state machine library or schema-enforced transition rules. The "NEVER query SPRINTS.db directly" rule is a social contract, not an enforced constraint.
- `schema.sql` was out of sync with the actual DB (missing `files_changed`, `lines_added`, `lines_removed` columns) — a fresh DB from schema would silently break sprint.sh and viewer.py. No migration tooling.
- SQL injection via `$repo` and `$branch` in sprint.sh's queries.

### Web Viewer Coupling

`viewer.py` has hardcoded assumptions about `SPRINTS.db` schema and the `runs/` directory layout on the same host as the orchestrator. It cannot be deployed separately or point to a remote DB.

---

## 7. The Slack Bot Architecture

### Transport: Socket Mode

The bot uses Slack's Socket Mode — the Python process initiates an outbound WebSocket to Slack. No public endpoint, no ngrok, no firewall rules needed. Uses the `slack-bolt` Python library:

```python
app = App(token=SLACK_BOT_TOKEN)
handler = SocketModeHandler(app, SLACK_APP_TOKEN)
handler.start()
```

### Intent Parsing: LLM-Based

Every incoming Slack message is sent to Claude Haiku with a dynamically-built prompt listing all available agents and their descriptions. Haiku returns `{task_type, parameters, confidence}`. Messages with confidence < 0.7 are ignored. Cost ~$0.0003 per parse, latency ~200-500ms.

Task descriptions are discovered at runtime from `agents/*/slack.md` and `prompt.md` frontmatter — adding a new agent automatically makes it available to the bot.

### Thread-Based Replies

All bot responses go into the Slack thread that originated the request. Users see: immediate acknowledgment ("Analysis started"), status updates (queue position), and final results (decision, summary, action buttons) — all in thread. The `thread_ts` from the original event is carried through the entire job lifecycle.

### Agent Pool: Pre-Warmed Containers

The pool manager starts N containers (default 2, max 5) at bot startup via `start_agent.sh`. Each container is named `agent-slack-N`. State:

- **READY** — container is running with workspace initialized, waiting for a job
- **BUSY** — running a job
- **RECYCLING** — being killed and recreated after job completion

When a job arrives and an agent is READY, it's assigned immediately. Otherwise the job queues. After a job completes, the agent is killed and recreated fresh (no state leakage between jobs). If any queued jobs exist when the fresh agent becomes READY, it picks up the next job immediately.

Job execution: `docker exec -i agent-slack-N bash /tmp/agent_runner.sh` with context piped to stdin. Outputs extracted via `docker cp`. Results posted back to Slack as Block Kit messages with decision-appropriate formatting.

### Retry / Continuation Flow

The Slack bot supports multi-turn workflows:
- **Retry (same agent):** User provides feedback in a thread; the bot re-runs the agent with prior outputs (prefixed `prior_*`) and feedback injected as `/agent_input/` files.
- **Continuation (different agent):** User asks to run a different agent (e.g., `research_agent` → `create_pr`); prior outputs are carried forward.
- **Validation:** Each agent's `validate_params()` function checks whether all required parameters are available before dispatching, providing friendly error messages if not.

### `slack.md` Per-Agent Integration Doc

Each Slack-integrated agent has a `slack.md` file that documents its interface for the LLM intent parser:
- What the agent does
- What parameters it accepts
- Example user messages and how they map to parameters
- What the agent outputs and how they appear in Slack

The intent parser ingests this file as part of the prompt so Haiku knows how to parse requests for that specific agent.

---

## Summary: Key Architectural Patterns to Carry Forward

| Pattern | What worked | What to improve |
|---------|------------|-----------------|
| **Universal runner** | Single `run_agent.sh` + per-agent `agent.py` interface is clean and extensible | Move from bash to Python for the runner; typed interfaces |
| **DinD isolation** | Per-agent Docker daemon prevents all port conflicts and state leakage; `down -v` is fully clean | Startup cost (~3min first boot) needs warm pool or OverlayFS |
| **OverlayFS copy-on-write** | ~50% reduction in cold-start time; per-agent isolation without disk waste | Requires `privileged` / `CAP_SYS_ADMIN`; needs careful testing |
| **decision.txt protocol** | Simple text file with a known set of values is easy for agents to write and orchestrators to read | Consider structured JSON for richer metadata |
| **docker cp output collection** | Clean security boundary; agent can't see host filesystem | One-way only; no mid-run streaming |
| **git patch as artifact** | Captures code changes in a portable, reviewable format | Need robust apply + retry; worktree approach is cleaner than in-place apply |
| **Volume-mounted overlay for shared state** | Sprint agents share plan/status without message passing | Tightly coupled to filesystem layout; doesn't scale to distributed agents |
| **SPRINTS.db state machine** | SQLite is simple and queryable; `sprint.sh` encapsulates transitions | Bash state machine is fragile; needs proper state machine library, migration tooling |
| **Reviewer-as-learning-system** | Session reports that accumulate across a run make each Runner iteration smarter | This pattern (separate analyze-and-decide role) should be a first-class abstraction |
| **Socket Mode Slack bot** | No infrastructure needed, pure outbound WebSocket | In-memory state is not durable; needs persistence layer for production |
| **LLM intent parsing** | Natural language dispatch with dynamic task discovery is very flexible | Confidence threshold tuning required; ambiguous requests need clarification flow |
| **Pre-warmed agent pool** | Eliminates per-job cold-start cost for interactive workflows | Pool size is static; needs auto-scaling based on queue depth |
