# Tool Guidelines

You have access to these CLI tools. Use them — they are your primary interface to the project's knowledge and work tracking.

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

## Working Style

- Research before building — search the graph for context before writing code
- Drop trail markers — `graph note` for pitfalls, insights, operational discoveries
- Link your work — `graph link <bead> <source> -r implemented_by`
- Stay focused — complete the assigned bead, don't scope-creep
- Report blockers — if you can't proceed, write a BLOCKED decision with details
