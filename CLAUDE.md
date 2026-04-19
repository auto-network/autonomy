# Autonomy Network

AGI platform project. See `musings/README.md` for founding vision docs.

## Project Structure

```
musings/              — vision docs, design notes, research reports
agents/               — agent infrastructure
  shared/             — shared prompt blocks (tool guidelines, experience report, etc.)
  templates/          — per-agent prompt templates
  Dockerfile          — base agent container image (planned)
tools/                — utility tools, each with its own TOOL.md
  graph/              — knowledge graph: SQLite + FTS5, CLI via `graph` command
  scraper/            — stealth browser + DOM extraction for ChatGPT/Claude.ai
  dashboard/          — web UI: bead board, session monitor, graph explorer (planned)
  analytics/          — session log analysis, tool use metrics (planned)
data/                 — extracted/collected data
  graph.db            — knowledge graph database (100K+ thoughts, gitignored)
  chatgpt/            — scraped ChatGPT conversations (.json, .md)
  claude/             — scraped Claude.ai conversations (.json, .md)
.beads/               — Beads issue tracker (Dolt-backed, local state gitignored)
.venv/                — Python 3.12 virtualenv
.browser_profile/     — persistent Chromium profile (login sessions survive restarts)
```

## Tools Index

| Tool | Path | CLI | Description |
|------|------|-----|-------------|
| Graph | `tools/graph/` | `graph <cmd>` | Knowledge graph with FTS5 search, 6000+ sources, project scoping |
| Scraper | `tools/scraper/` | HTTP REPL :8765 | Stealth browser + DOM extraction for ChatGPT/Claude.ai |
| Dashboard | `tools/dashboard/` | `dashboard-mock` | Web UI for beads, sessions, graph, monitoring |
| Analytics | `tools/analytics/` | (planned) | Session log analysis, tool use metrics |

Each tool has a `TOOL.md` describing its purpose, usage, and architecture.

## Key CLIs

### Knowledge Graph (`graph`)
| Command | What | Example |
|---------|------|---------|
| `graph search "query"` | Full-text search (use `--or` for OR mode) | `graph search "CVSS fuzzing" --project enterprise-ng` |
| `graph search "query" --or` | Match ANY term instead of all | `graph search "auth login session" --or` |
| `graph read <src_id>` | Read full source content | `graph read dc4c73ee --max-chars 2000` |
| `graph read <src_id> --save <path>` | Export raw content to file for editing | `graph read abc123 --save /tmp/notes/abc123.md` |
| `graph context <src_id> <turn>` | Show turns around a search hit | `graph context 8cdc1d85 286 --window 3` |
| `graph context <src_id> last` | Show latest turns of a source | `graph context 8cdc1d85 last --window 5` |
| `graph sources` | List sources | `graph sources --project jira --type docs` |
| `graph sources --verbose` | List sources with file paths | `graph sources -v --limit 5` |
| `graph projects` | Show all projects with source counts | |
| `graph attention` | Show human input chronologically | `graph attention --last 10` |
| `graph note "text"` | Drop a searchable trail marker | `graph note "pitfall: X breaks Y" --tags pitfall` |
| `graph note "text" --attach <file>` | Create note with file attachment | `graph note "Screenshot: ![desc]({1})" --attach /tmp/shot.png` |
| `graph note -c - --attach ...` | Multi-attachment note (use `![alt]({1})`, `![alt]({2})`) | `graph note -c - --attach img1.png --attach img2.png < note.txt` |
| `graph attach <file>` | Store file as graph attachment | `graph attach /tmp/screenshot.png --source 7bf1d812 --turn 5` |
| `graph attachment <id>` | Show attachment metadata | `graph attachment 7c0c8c82` |
| `graph attachments [source_id]` | List attachments | `graph attachments 45869b69` |
| `graph comment <src_id> "text"` | Add a comment to a note | `graph comment f6c6c43e "fix step 3 wording"` |
| `graph comment integrate <id>` | Mark comment as rolled into note body | `graph comment integrate df5f1546` |
| `graph note update <src_id>` | Update a note (versioned, non-destructive) | `graph note update f6c6c43e -c - --integrate df5f < new.txt` |
| `graph read <src_id>@N` | Read a specific version of a note | `graph read f6c6c43e@1` |
| `graph read <src_id>@` | List all versions with timestamps | `graph read f6c6c43e@` |
| `graph link <bead> <src>` | Create provenance edge | `graph link auto-5kj 8cdc1d85 -r conceived_at -t 286` |
| `graph bead "title"` | Create bead with provenance link | `graph bead "Fix X" --source 8cdc1d85 --turns 286` |
| `graph agent-runs` | Discover and ingest subagent traces | `graph agent-runs --list` |
| `graph sessions --all` | Ingest latest session data (107ms) | Run before searching for recent content |
| `graph sessions --status` | Compact session status table (live-only; add `--since` to include recent dead sessions) | `graph sessions --status --since 12h` |
| `graph wait <bead-id>` | Block until a dispatched bead completes | `graph wait auto-x7wr --timeout 900` |
| `graph dispatch` | Show running/queued dispatch state | `graph dispatch runs --failed` |
| `graph dispatch status <bead-id>` | Post-dispatch detail: decision, experience, session links | `graph dispatch status auto-yz29` |
| `graph dispatch runs --completed` | Filter to completed (DONE) runs only | `graph dispatch runs --completed --limit 10` |
| `graph dispatch runs --primer` | Rich per-run output: title, commit, diff, scores, merge state | `graph dispatch runs --primer --completed` |
| `graph primer <bead-id>` | Dynamic context primer for a bead (description + pitfalls + provenance) | `graph primer auto-n9qa` |
| `graph ui-design "title" <dir>` | Create Design Studio design from HTML files + live-watch for changes | `graph ui-design "Input redesign" /tmp/cards/` |
| `graph set-label "text"` | Set a working title for the current session | `graph set-label "Passkey auth design"` |
| `graph notes --since <dur>` | List notes by recency with duration filter | `graph notes --since 1h --tags pitfall` |
| `graph crosstalk` | CrossTalk message log (default) | `graph crosstalk --since 1h --session auto-0323-022132` |
| `graph crosstalk send <target> "msg"` | Send message to a session | `graph crosstalk send auto-0325-123456 "check this"` |
| `graph crosstalk broadcast "msg"` | Send message to all live sessions | `graph crosstalk broadcast "deploy in 5m"` |
| `graph dispatch stats` | Aggregate statistics: success rate, tooling scores, top failures | `graph dispatch stats --since 7d` |
| `graph dispatch stats --trend` | Weekly trend with direction indicators | `graph dispatch stats --trend` |
| `graph dispatch stats --by-image` | Break down stats by container image | `graph dispatch stats --by-image` |
| `graph dispatch watch` | Block until next dispatch completes | `graph dispatch watch --timeout 300` |
| `graph collab topics` | List tags with descriptions and note counts | `graph collab topics` |
| `graph collab tag-describe <tag> "desc"` | Set or update a tag description | `graph collab tag-describe pitfall "Operational hazards..."` |
| `graph thought "text"` | Capture a raw idea with optional provenance | `graph thought "auth needs passkeys" --tags auth` |
| `graph thoughts` | List recent thoughts | `graph thoughts --since 1h` |
| `graph thread create "title"` | Create a thought thread | `graph thread create "Passkey auth design" -p 1` |
| `graph thread park <id>` | Park a thread | `graph thread park abc123` |
| `graph thread done <id>` | Mark thread as done | `graph thread done abc123` |
| `graph thread assign <cap> <thr>` | Assign capture to thread | `graph thread assign cap123 thr456` |
| `graph thread` / `graph threads` | List active threads | `graph threads --all` |

### Beads (`bd`)
| Command | What | Example |
|---------|------|---------|
| `bd ready` | Show beads with no blockers | |
| `bd show <id>` | Show bead details | `bd show auto-5kj` |
| `graph bead "title"` | Create bead with provenance link (preferred over `bd create`) | `graph bead "Fix X" -p 1 --source 8cdc1d85 --turns 286 -d - < desc.txt` |
| `bd dep tree <id>` | Show dependency tree | |
| `bd close <id> --reason "..."` | Close a completed bead | |

### Mock Dashboard (`dashboard-mock`)

Invoke as `python3 -m tools.dashboard.mock_server` or `./tools/dashboard/bin/dashboard-mock`.

| Command | What | Example |
|---------|------|---------|
| `dashboard-mock start` | Start mock server (self-daemonizing) | `dashboard-mock start --port 8082` |
| `dashboard-mock start --fixture <path>` | Start with custom fixture data | `dashboard-mock start --port 8082 --fixture /tmp/f.json` |
| `dashboard-mock stop` | Stop mock server via PID file | `dashboard-mock stop --port 8082` |
| `dashboard-mock status` | Check if mock server is running | `dashboard-mock status --port 8082` |

## Workflows

### When you discover something important during a conversation
```bash
graph sessions --all                          # refresh (107ms)
graph search "the key phrase"                 # find the turn
graph bead "Title" -p 1 \                     # create bead with provenance
  --source <src_id> --turns <N> \
  -d - < /tmp/desc.txt                         # use -d - for long descriptions (avoids shell quoting)
```

### When you learn a pitfall or operational insight
```bash
graph note "description of the pitfall" --tags pitfall,topic --project autonomy
```

### When updating a living note
```bash
graph read <src_id> --all-comments           # current content + all comments
# Write a clean new version synthesizing everything
graph note update <src_id> -c - --integrate <cid1> --integrate <cid2> < /tmp/revised.txt
```

### When making surgical edits to a note
```bash
graph read <src_id> --save /tmp/notes/<src_id>.md   # export raw content
# ... use Edit tool on the file ...
graph note update <src_id> -c - < /tmp/notes/<src_id>.md   # push changes back
```

Note: `graph note` and `graph note update` auto-save to `/tmp/graph-notes/{source_id}.md`.

### Key References

Read these graph notes to orient — use `graph read <id>` to load any of them.

| Note | ID | What it covers |
|------|----|----------------|
| Signpost Index | `38c10838-094` | Master index of all architectural notes — start here to find anything |
| Host Operations Protocol | `098a5407-39d` | Verification protocol, service management, merge handling, CrossTalk |
| Bead Polishing Protocol | `f6c6c43e-24a` | How to formulate and refine beads before dispatch |
| Note Revision Protocol | `843a8137-3c7` | How to update graph notes (versioning, comment integration) |
| Dispatch Lifecycle | `c706c9f3-5a8` | State machine, failure classification, recovery, merge flow |
| Testing Architecture | `527150ad-743` | L1/L2 test tiers, validation patterns |
| Design Studio Guide | `225a4af7-ee5` | Fixture states, responsive patterns, design-to-production workflow |

### Before working on a bead — get the full primer
```bash
graph primer <bead-id>                        # description + pitfalls + provenance turns
# Read the primer BEFORE writing code — pitfalls often contain exact solutions
```

### Before creating any bead — research first
```bash
graph search "topic" --or --limit 10          # mine the graph for context
graph read <src_id> --max-chars 3000          # read full sources
# THEN create the bead with informed context
```

### Checking human attention trail
```bash
graph attention --last 20                     # recent human input
graph attention --search "keyword"            # find when user discussed something
```

### Session self-management
```bash
graph set-label "topic description"                        # working title
graph set-topics "Status line 1" "Status line 2"           # card status lines
graph set-role researcher                                  # session role
graph set-nag --interval 10 --message "Check in"           # idle nag
graph set-nag --dispatch                                   # notify on every dispatch completion
graph set-nag --off                                        # disable idle nag
graph set-nag --dispatch --off                             # disable dispatch nag
graph crosstalk send <session> "message"                   # send message
graph crosstalk send <session> -c - < /tmp/msg.txt         # pipe long message
graph crosstalk broadcast "message"                        # send to all live sessions
graph sessions --status                                    # live-only status table
graph sessions --since 12h --status                        # include dead sessions active in the last 12h (post-mortem)
```

## Environment

- Python 3.12 via `.venv/`
- Go tools in `~/go/bin/` (`bd`, `dolt`)
- WSL2 on Windows, browser visible via WSLg (`DISPLAY=:0`)
- Key packages: `scrapling[all]`, `markdownify`, `beautifulsoup4`
- File handoffs between container and host sessions go through `/workspace/output/` (container) → `data/agent-runs/<session-name>/` (host). See the "File handoff to host-side tools" block in `agents/shared/terminal/CLAUDE.md` for details.

## Testing convention

Always tee test output: `pytest ... 2>&1 | tee /tmp/test-results.txt`. Never re-run to re-read.

## Conventions

- Tools live under `tools/<name>/` with a `TOOL.md`
- Design docs and research go in `musings/`
- Agent prompt templates go in `agents/`
- Extracted data goes under `data/<source>/`
- Work tracking via `bd` (Beads) — all tasks are beads
- Research before building — mine the graph for context before creating beads
- Scoped agent access via `GRAPH_SCOPE` env var and `graph-<project>` wrappers
- Set your session label when starting a new topic: `graph set-label "topic description"`

## Naming: Design Studio (formerly "experiments")

The Design Studio was previously called "experiments" in the codebase.
- URL: /design/{design_id} (was /experiments/)
- API: /api/design/ (was /api/experiments/)
- CLI: graph ui-design (was graph ui-exp)
- Data: design_id (was series_id), revisions (was sibling_ids), revision_seq (was series_seq)

If you encounter "experiments", "series_id", "sibling_ids", or /experiments/ in code or docs, these are stale.
