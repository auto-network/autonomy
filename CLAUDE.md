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
  graph.db            — knowledge graph database (92K+ thoughts, gitignored)
  chatgpt/            — scraped ChatGPT conversations (.json, .md)
  claude/             — scraped Claude.ai conversations (.json, .md)
.beads/               — Beads issue tracker (Dolt-backed, local state gitignored)
.venv/                — Python 3.12 virtualenv
.browser_profile/     — persistent Chromium profile (login sessions survive restarts)
```

## Tools Index

| Tool | Path | CLI | Description |
|------|------|-----|-------------|
| Graph | `tools/graph/` | `graph <cmd>` | Knowledge graph with FTS5 search, 4600+ sources, project scoping |
| Scraper | `tools/scraper/` | HTTP REPL :8765 | Stealth browser + DOM extraction for ChatGPT/Claude.ai |
| Dashboard | `tools/dashboard/` | (planned) | Web UI for beads, sessions, graph, monitoring |
| Analytics | `tools/analytics/` | (planned) | Session log analysis, tool use metrics |

Each tool has a `TOOL.md` describing its purpose, usage, and architecture.

## Key CLIs

### Knowledge Graph (`graph`)
| Command | What | Example |
|---------|------|---------|
| `graph search "query"` | Full-text search (use `--or` for OR mode) | `graph search "CVSS fuzzing" --project enterprise-ng` |
| `graph search "query" --or` | Match ANY term instead of all | `graph search "auth login session" --or` |
| `graph read <src_id>` | Read full source content | `graph read dc4c73ee --max-chars 2000` |
| `graph context <src_id> <turn>` | Show turns around a search hit | `graph context 8cdc1d85 286 --window 3` |
| `graph sources` | List sources | `graph sources --project jira --type docs` |
| `graph sources --verbose` | List sources with file paths | `graph sources -v --limit 5` |
| `graph projects` | Show all projects with source counts | |
| `graph attention` | Show human input chronologically | `graph attention --last 10` |
| `graph note "text"` | Drop a searchable trail marker | `graph note "pitfall: X breaks Y" --tags pitfall` |
| `graph note "text" --attach <file>` | Create note with file attachment | `graph note "Bug {1}" --attach /tmp/shot.png` |
| `graph comment <src_id> "text"` | Add a comment to a note | `graph comment f6c6c43e "fix step 3 wording"` |
| `graph comment integrate <id>` | Mark comment as rolled into note body | `graph comment integrate df5f1546` |
| `graph note update <src_id>` | Update a note (versioned, non-destructive) | `graph note update f6c6c43e -c - --integrate df5f < new.txt` |
| `graph read <src_id>@N` | Read a specific version of a note | `graph read f6c6c43e@1` |
| `graph read <src_id>@` | List all versions with timestamps | `graph read f6c6c43e@` |
| `graph link <bead> <src>` | Create provenance edge | `graph link auto-5kj 8cdc1d85 -r conceived_at -t 286` |
| `graph bead "title"` | Create bead with provenance link | `graph bead "Fix X" --source 8cdc1d85 --turns 286` |
| `graph agent-runs` | Discover and ingest subagent traces | `graph agent-runs --list` |
| `graph sessions --all` | Ingest latest session data (107ms) | Run before searching for recent content |
| `graph wait <bead-id>` | Block until a dispatched bead completes | `graph wait auto-x7wr --timeout 900` |
| `graph dispatch` | Show running/queued dispatch state | `graph dispatch runs --failed` |
| `graph dispatch runs --completed` | Filter to completed (DONE) runs only | `graph dispatch runs --completed --limit 10` |
| `graph dispatch runs --primer` | Rich per-run output: title, commit, diff, scores, merge state | `graph dispatch runs --primer --completed` |
| `graph primer <bead-id>` | Dynamic context primer for a bead (description + pitfalls + provenance) | `graph primer auto-n9qa` |
| `graph ui-exp "title" <dir>` | Create UI experiment from HTML files + live-watch for changes | `graph ui-exp "Input redesign" /tmp/cards/` |
| `graph set-label "text"` | Set a working title for the current session | `graph set-label "Passkey auth design"` |

### Beads (`bd`)
| Command | What | Example |
|---------|------|---------|
| `bd ready` | Show beads with no blockers | |
| `bd show <id>` | Show bead details | `bd show auto-5kj` |
| `graph bead "title"` | Create bead with provenance link (preferred over `bd create`) | `graph bead "Fix X" -p 1 --source 8cdc1d85 --turns 286 -d - < desc.txt` |
| `bd dep tree <id>` | Show dependency tree | |
| `bd close <id> --reason "..."` | Close a completed bead | |

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

### Bead polishing protocol
Read the full protocol before polishing any bead:
`graph://f6c6c43e-24a`  (resolves to: `graph read f6c6c43e-24a`)
Note revision protocol: `graph://843a8137-3c7`

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

## Environment

- Python 3.12 via `.venv/`
- Go tools in `~/go/bin/` (`bd`, `dolt`)
- WSL2 on Windows, browser visible via WSLg (`DISPLAY=:0`)
- Key packages: `scrapling[all]`, `markdownify`, `beautifulsoup4`

## Conventions

- Tools live under `tools/<name>/` with a `TOOL.md`
- Design docs and research go in `musings/`
- Agent prompt templates go in `agents/`
- Extracted data goes under `data/<source>/`
- Work tracking via `bd` (Beads) — all tasks are beads
- Research before building — mine the graph for context before creating beads
- Scoped agent access via `GRAPH_SCOPE` env var and `graph-<project>` wrappers
- Set your session label when starting a new topic: `graph set-label "topic description"`
