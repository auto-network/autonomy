# Agent Lifecycle

The complete dispatch cycle from bead to completion. This is the core protocol — all autonomous agents follow it.

## Overview

```
Dispatcher                          Container
──────────                          ─────────
1. Claim bead
2. Create worktree (agent/<bead>)
3. Generate prompt (primer + shared blocks)
4. Launch container ──────────────→ 5. Agent receives prompt
   worktree at /workspace/repo        Reads, edits, commits
   output at /workspace/output        Writes decision.json
                                      Writes experience_report.md
                                   6. Agent exits ←────────────
7. Collect commit hash
8. Record commit on bead
9. Process decision
   DONE → close bead
   BLOCKED → mark blocked + reason
   FAILED → mark failed + reason
10. Create discovered beads
11. Clean up worktree
    (branch persists for review)
```

## Phase 1: Pre-Launch (Dispatcher)

**Claim:** `bd set-state <bead> work=claimed --reason "dispatcher:<pid>"`

**Worktree:** `git worktree add .worktrees/<bead>-<timestamp> agent/<bead>`
- Isolated copy of the repo on a dedicated branch
- Mounted read-write into container at `/workspace/repo`
- Agent commits go on this branch, not main

**Image Selection:** Dispatcher reads bead labels and routes to the right container image via `LABEL_IMAGE_MAP`:
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

**Collect results:**
- Read commit hash from worktree (did the agent commit?)
- Read `decision.json` from output directory
- Read `experience_report.md`

**Record on bead:**
- Append commit hash: `bd update <bead> --append-notes "commit: <hash> branch: agent/<bead>"`
- Append agent notes from decision

**Process decision:**
| Decision | Action |
|----------|--------|
| `DONE` | `bd close <bead> --reason <reason>` |
| `BLOCKED` | `bd set-state <bead> work=blocked --reason <reason>` |
| `FAILED` | `bd set-state <bead> work=failed --reason <reason>` |

**Create discovered beads:** Any new work the agent found gets created from `discovered_beads` array.

**Ingest session:** `graph sessions --all` to capture the agent's session in the knowledge graph.

**Cleanup:** `git worktree remove <path>` — worktree deleted, branch persists for review/merge.

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

## Files

| File | Role |
|------|------|
| `agents/dispatcher.py` | Deterministic dispatch loop |
| `agents/launch.sh` | Worktree + container lifecycle |
| `agents/compose.py` | Prompt generation |
| `agents/shared/tool_guidelines.md` | Agent instructions |
| `agents/shared/experience_report.md` | Feedback template |
| `agents/Dockerfile` | Base container image |
| `agents/images/<project>/Dockerfile` | Project-specific images |
| `agents/build.sh` | Image build script |
