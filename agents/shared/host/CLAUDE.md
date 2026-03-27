# Host Agent — Environment Brief

You are running natively on the host machine as the user, not inside a container.
This is an open-ended interactive session. There is no bead, no task directive, and no `decision.json` to write.

## Access
- `/workspace/repo` is **read-write** — you can edit files, commit, and push
- Docker is available — you can launch agent containers
- `~/.claude/` is persistent on the host, but graph is still the right store for cross-agent knowledge

## Capabilities

### graph — Knowledge Graph
100K+ thoughts, 6000+ sources, full-text search. Primary tool for orienting around any topic.

```bash
graph search "query"                  # FTS search (--or for ANY term)
graph search "query" --project jira   # scoped to a project
graph attention --last 20             # human focus trail
graph read <src_id> --max-chars 3000  # read a full source
graph context <src_id> <turn>         # turns around a search hit
graph sessions --all                  # ingest latest session data before searching
graph note "text" --tags tag          # persist an insight or pitfall for future agents
graph bead "title" --source <id>      # create a bead with provenance
graph primer <bead-id>                # full context primer: description + pitfalls + provenance
graph dispatch approve <bead-id>      # approve bead(s) for dispatch (accepts multiple IDs)
graph dispatch status                 # compact one-liner
graph dispatch runs                   # running/queued agent activity
graph wait <bead-id>                 # block until bead completes (background it in your shell)
graph context <src_id> last             # latest turns (no turn number needed)
graph sessions --status                  # live session table from dashboard
graph notes --since 1h                   # recent notes by time
graph crosstalk --since 1h               # recent CrossTalk messages
graph thought "text" --tags tag          # capture a raw idea
graph thread "title"                     # create a thought thread
graph collab topics                      # browse tag taxonomy with descriptions
graph set-label "title"                  # set session working title
graph set-topics "Line 1" "Line 2"       # set card status lines (1-4)
graph set-role researcher                # set session role
graph set-nag --interval 10              # enable idle nag (--off to disable)
graph crosstalk send <s> "msg"           # send CrossTalk message (-c - for stdin)
```
Run `graph --help` for full reference.

**Dispatch workflow:**
1. `graph dispatch approve <bead-id>` — release bead for dispatch
2. `graph wait <bead-id>` — block until dispatched bead completes
3. `graph dispatch status` — check overall queue at any time

### bd — Beads Issue Tracker
Work tracking. Dolt-backed, read-write.

```bash
bd ready                     # beads with no blockers
bd show <id>                 # bead details
bd dep tree <id>             # dependency tree
bd close <id> --reason "…"  # close a completed bead
```
Run `bd --help` for full reference.

### agent-browser — Headless Chrome
Pre-configured: dark mode, PNG screenshots to `/tmp/screenshots/`, `--no-sandbox`.
```bash
agent-browser open https://localhost:8080 --ignore-https-errors
agent-browser snapshot -i             # interactive elements with refs
agent-browser screenshot --annotate   # labeled visual screenshot
agent-browser eval "document.title"   # run JS in page context
```
Run `agent-browser --help` for full reference.
See `agents/shared/dashboard/agent-browser-primer.md` for dashboard-specific patterns.

### Docker & Agent Launch
```bash
agents/launch.sh <bead-id>            # launch agent container for a bead (foreground)
agents/launch.sh <bead-id> --detach   # launch in background (dispatcher manages)
agents/build.sh                       # rebuild the autonomy-agent image
```

### Service Management
Dashboard hot-reloads Python changes automatically. Full restart only needed for config/env changes:
```bash
tools/dashboard/start-dashboard.sh --status    # check if running
tools/dashboard/start-dashboard.sh --restart   # full restart
tools/dashboard/start-dashboard.sh --stop      # stop

agents/start-dispatcher.sh --status   # check if running
agents/start-dispatcher.sh --restart  # restart dispatcher
agents/start-dispatcher.sh --stop     # stop
```

### Host Network
All localhost services reachable directly:
- Dashboard: `https://localhost:8080`

## Persistence
- **graph** is the cross-agent memory: `graph note` for insights/pitfalls, `graph bead` for work items
- `~/.claude/` is persistent on the host but is personal/local — not visible to container agents

## Bead Polishing Protocol
When formulating or refining beads, read the protocol directly:
`graph://f6c6c43e-24a`  (resolves to: `graph read f6c6c43e-24a`)

## Working Style
- In design discussions, your role is to formulate and polish beads — not to dispatch or implement while the design is ongoing. The user will signal when work is ready to dispatch.
- Orient yourself using the tools above **after** the user tells you what they need — not as a startup ritual.
- You have full write access — be deliberate with destructive operations (force push, dropping DBs, killing the dispatcher, deleting worktrees).
