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

| Command | What | Example |
|---------|------|---------|
| `graph search "query"` | Full-text search across knowledge graph | `graph search "CVSS fuzzing" --project enterprise-ng` |
| `graph read <src_id>` | Read full source content | `graph read dc4c73ee --max-chars 2000` |
| `graph sources` | List sources | `graph sources --project jira --type docs` |
| `graph projects` | Show all projects with source counts | |
| `bd ready` | Show beads with no blockers | |
| `bd show <id>` | Show bead details | `bd show auto-5kj` |
| `bd create "title" -p N` | Create a bead | `bd create "Fix bug" -t task -p 1` |
| `bd dep tree <id>` | Show dependency tree | |

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
