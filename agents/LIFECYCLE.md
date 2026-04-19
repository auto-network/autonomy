# Agent Lifecycle

The complete dispatch cycle from bead to completion. This is the core protocol — all autonomous agents follow it.

## Overview

```
Dispatcher                          Container
──────────                          ─────────
1. Query approved beads
2. Claim bead (set in_progress)
3. Save branch base hash
4. Create worktree (agent/<bead>)
5. Select container image by label
6. Generate prompt (primer + shared)
7. Launch container ──────────────→ 8. Agent receives prompt
   worktree at /workspace/repo        Reads, edits, commits
   output at /workspace/output        Writes decision.json
   sessions at host volume             Writes experience_report.md
                                    9. Agent exits ←────────────
10. Detect new commits (vs saved base)
11. Record commit hash on bead
12. Auto-merge agent branch to master
    (or mark BLOCKED on conflict)
13. Process decision
    DONE → close bead
    BLOCKED → mark blocked + reason
    FAILED → mark failed + reason
14. Create discovered beads
15. Ingest session into graph
16. Link session to bead (implemented_by)
17. Clean up worktree + branch
```

## Approval Gate

Beads require human approval before dispatch. In the dashboard, click "Approve for Dispatch" on a bead detail page. This sets `readiness=approved` via `bd set-state`, which is the single gate the dispatcher queries.

**Three orthogonal state dimensions:**
- **status**: open, in_progress, blocked, deferred, closed — work execution state
- **readiness**: idea, draft, specified, approved — spec maturity, human-gated
- **dispatch**: queued, launching, running, collecting, merging, done, failed — automation pipeline state

The dispatcher queries `status=open AND label=readiness:approved` — a single condition with no duplicate checks.

## Phase 1: Pre-Launch (Dispatcher)

**Claim:** `bd update <bead> -s in_progress` + `bd set-state <bead> work=claimed` + `bd set-state <bead> dispatch=queued`

**Dispatch states:** The dispatcher walks the dispatch dimension through each phase:
`queued → launching → running → collecting → merging → done` (or `failed` on error)

**Branch base:** Save `git rev-parse HEAD` before agent runs — used to detect new commits after.

**Worktree:** `git worktree add .worktrees/<bead>-<timestamp> agent/<bead>`
- Isolated copy of the repo on a dedicated branch
- Mounted read-write into container at `/workspace/repo`
- `.git` directory mounted at same absolute path so worktree references resolve

**Image Selection:** Dispatcher reads bead labels and routes to the container image declared by the matching project in `agents/projects.yaml` (`dispatch_labels` → `image`). Beads with no matching project label fall through to the rig default from `.beads/config.yaml`. The same lookup also supplies `graph_project` and `default_tags`, which become `GRAPH_SCOPE` / `GRAPH_TAGS` in the agent container and are written into `.session_meta.json`.

- `autonomy-agent` — base image for research/refinement
- `autonomy-agent:dashboard` — adds starlette, uvicorn for dashboard work
- More project images as needed

**Prompt:** Generated dynamically from the graph by `agents/compose.py`:
- Bead description + acceptance criteria
- Provenance turns from original discussions
- Related graph notes and pitfalls
- Shared instruction blocks (tool guidelines, experience report template)
- Dispatcher directives

## Phase 2: Execution (Agent in Container)

The agent runs as Claude Code with `--dangerously-skip-permissions --print`.

**Available to the agent:**
- `/workspace/repo` — git worktree, read-write, agent commits here
- `/workspace/output` — persists after container exit (decision, reports)
- Session JSONL — written to host via volume mount at `data/agent-runs/<bead>-<ts>/sessions/`
- `graph` CLI — read-only access to knowledge graph
- `bd` CLI — read-only access to beads
- `git` — full access within the worktree
- Project-specific tools (uvicorn, etc.) if using a project image

**Agent responsibilities:**
1. Read the task from the prompt
2. Research context in the graph
3. Do the work in `/workspace/repo`
4. Commit changes on the worktree branch
5. Write `/workspace/output/decision.json`
6. Write `/workspace/output/experience_report.md`

**Agent constraints:**
- Cannot modify beads (`bd --readonly`)
- Cannot push to remote (no SSH keys, no push credentials)
- Cannot access other worktrees or the main working tree
- 10 minute timeout (configurable)

## Phase 3: Post-Execution (Dispatcher)

**Detect commits:** Compare worktree HEAD against saved branch base. If different, agent committed.

**Record on bead:**
- Append commit hash: `bd update <bead> --append-notes "commit: <hash> branch: agent/<bead>"`
- Append agent notes from decision

**Auto-merge:** If decision is DONE and agent committed:
- `git merge agent/<bead>` into master
- Record merge hash on bead
- On conflict: abort merge, mark bead BLOCKED for manual review

**Process decision:**
| Decision | Action |
|----------|--------|
| `DONE` | `bd close <bead> --reason <reason>` |
| `BLOCKED` | `bd set-state <bead> work=blocked --reason <reason>` |
| `FAILED` | `bd set-state <bead> work=failed --reason <reason>` |

**Create discovered beads:** Any new work the agent found gets created from `discovered_beads` array.

**Ingest + link session:**
1. `graph sessions --all` — ingest the session JSONL into the knowledge graph
2. Find the session source by UUID
3. `graph link <bead> <session-source> -r implemented_by`

**Cleanup:**
- `git worktree remove <path>` — worktree deleted
- `git branch -d agent/<bead>` — branch deleted (if merged successfully)

## Structural Enforcement

The protocol is enforced structurally, not behaviorally:

| Constraint | Mechanism |
|------------|-----------|
| Agent can't skip claiming | Dispatcher owns claim, agent doesn't start without it |
| Agent can't modify beads | `bd --readonly` in container |
| Agent can't push to remote | No SSH keys or push credentials in container |
| Agent can't pollute main tree | Worktree isolation — different branch, different directory |
| Agent must produce decision | Dispatcher marks FAILED if no decision.json |
| Agent can't exceed time limit | `subprocess.run(timeout=600)` kills the container |
| Commits must merge cleanly | Conflict → BLOCKED, not forced |

## Dashboard Integration

The Dispatch page (`/dispatch`) shows the full lifecycle:

- **Active Dispatches** — beads with dispatch state (queued/launching/running/collecting/merging), with container info
- **Approved — Waiting** — beads with `readiness:approved` not yet picked up
- **Last Runs** — completed dispatches with status indicators (DONE/BLOCKED/FAILED, merged/conflict)
- **Trace View** — click a completed run to see: decision, discovered beads, git diff, experience report

## Files

| File | Role |
|------|------|
| `agents/dispatcher.py` | Deterministic dispatch loop |
| `agents/launch.sh` | Worktree + container lifecycle |
| `agents/compose.py` | Prompt generation |
| `agents/readiness.py` | Readiness gate (idea/draft/specified/approved) |
| `agents/shared/tool_guidelines.md` | Agent instructions |
| `agents/shared/experience_report.md` | Feedback template |
| `agents/Dockerfile` | Base container image |
| `agents/images/<project>/Dockerfile` | Project-specific images |
| `agents/build.sh` | Image build script |
| `agents/LIFECYCLE.md` | This document |
