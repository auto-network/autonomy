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
```

## Beads (`bd`)

You are running in **read-only mode**. You cannot modify beads directly.
The dispatcher manages bead state on your behalf based on your decision file.

```
bd show <id>                      # View bead details
bd ready                          # See unblocked work
bd search "query"                 # Search beads
bd dep tree <id>                  # View dependency tree
```

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
- `tooling` — infrastructure experience (bd, graph, git, container, deps). 5 = smooth, 1 = constant fights.
- `clarity` — was the task well-specified enough to execute? 5 = crystal clear, 1 = had to guess everything.
- `confidence` — how solid is the delivered solution? 5 = production-ready, 1 = barely a sketch.

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

## Working Style

- Work in `/workspace/repo` — edit files, commit your changes
- Write decision and reports to `/workspace/output/` — this persists after container exit
- Research before building — search the graph for context before writing code
- Drop trail markers — `graph note` for pitfalls, insights, operational discoveries
- Commit your work — the dispatcher records your commit hash on the bead
- Stay focused — complete the assigned bead, don't scope-creep
- Report blockers — if you can't proceed, write a BLOCKED decision with details
