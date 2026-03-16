# Autonomy Dashboard

## Vision

The operational visibility layer for the Autonomy Network. A browser-based interface that lets you observe, navigate, and interact with the full state of the system — beads, knowledge graph, agent sessions, playbooks, and the relationships between them.

The dashboard follows the Autonomy UI philosophy: **content is markdown, hierarchy drives layout, the system tolerates drift.** Every view is a query into structured data rendered through a shared markdown engine. Adding a new data type doesn't require a new view — it renders automatically through the hierarchy.

## Core Principle: Recursive Zoom

The dashboard operates at every level of zoom using the same toolset:

- **Global view:** All projects, all beads, all sessions — the 30,000-foot picture
- **Project view:** One project's beads, sessions, sources, entities
- **Bead view:** One bead's full context — description, dependencies, linked sessions, results
- **Source view:** One source's full content — rendered markdown, turn-by-turn
- **Paragraph view:** One paragraph with annotations, cross-references, comments

At every level, you can: **search, read, annotate, link, and drill deeper.** The same sidebar, the same search box, the same rendering engine. Only the scope changes.

This maps directly to `GRAPH_SCOPE` — each zoom level is a scope. An agent working at the bead level sees the same interface as a human browsing at the project level. The dashboard is the human surface of what agents see through the CLI.

## Goals

### Immediate (Sprint 1)
- See the bead board: what's ready, what's blocked, what's in progress
- See active and recent agent sessions with live output
- Search and read the knowledge graph in a browser
- Render any markdown source with proper formatting

### Near-term
- Annotate past sessions at the paragraph level
- Trace a bead from creation through dispatch to completion
- Monitor agent token consumption and tool use efficiency
- View dependency graphs visually

### Aspirational
- Generative views: describe what you want to see, the system renders it
- Real-time agent observation with approval/intervention controls
- Collaborative annotations across sessions and users
- The dashboard IS the Autonomy Surface layer — not a separate tool

## Architecture

### Data Flow
```
bd ready --json          ──→  Bead Board
graph search --json      ──→  Knowledge Explorer
graph sources --json     ──→  Session Monitor
graph read <id>          ──→  Source Reader / Markdown Viewer
bd show <id> --json      ──→  Bead Detail
```

The dashboard is a **thin rendering layer over the CLI tools.** It calls `bd` and `graph` as subprocesses, parses their JSON output, and renders it. No direct database access. This means:
- Every view the dashboard shows, an agent can also produce via CLI
- The CLI is the source of truth; the dashboard is a lens
- New CLI capabilities automatically become available in the dashboard

### Stack
- **Backend:** Starlette + uvicorn (already in .venv)
- **Frontend:** Tailwind CSS (CDN), vanilla JS, marked.js for markdown
- **Rendering:** Client-side markdown rendering with syntax highlighting
- **Data:** JSON API endpoints that shell out to `bd` and `graph`
- **No framework:** No React, no Vue, no build step. HTML + JS + CSS.

### Recursive Component Model

Each view is a **component** that can render at any zoom level:

```
SourceList          — renders a list of sources (filterable by project, type)
  └→ SourceDetail   — renders one source's full content as markdown
       └→ TurnView  — renders one turn with annotation sidebar
            └→ Annotation — renders one annotation thread

BeadBoard           — renders the ready queue / kanban / dependency tree
  └→ BeadDetail     — renders one bead with full context, linked sources
       └→ SessionTrace — renders the dispatch→execution→result chain

SearchResults       — renders search hits with snippets
  └→ SourceDetail   — (same component, reused)
```

Components compose recursively. `SourceDetail` appears inside `SearchResults`, inside `BeadDetail`, inside `SessionTrace`. It's the same component every time — only the data changes.

## Sub-Components

Each of these will get its own specification document as we build them:

| Component | Spec | Bead | Status |
|-----------|------|------|--------|
| [Server Skeleton](spec/server.md) | Server, routes, static files, base layout | auto-5kj | planned |
| [Bead Board](spec/bead-board.md) | Ready queue, kanban, dependency DAG | auto-gmn | planned |
| [Session Monitor](spec/session-monitor.md) | Live/recent sessions, token burn | auto-wrh | planned |
| [Knowledge Explorer](spec/knowledge-explorer.md) | Search, entity browser, project filter | auto-6jf | planned |
| [Markdown Viewer](spec/markdown-viewer.md) | Source reader, syntax highlighting | auto-10s | planned |
| [Trace View](spec/trace-view.md) | Bead lifecycle chain | auto-fb1 | planned |
| [Annotation Interface](spec/annotations.md) | Paragraph-level comments | auto-8uf | planned |

## What This Is NOT

- Not a replacement for the CLI — the CLI is primary, the dashboard is a lens
- Not a SPA framework project — no build step, no node_modules, no bundler
- Not a generic dashboard toolkit — it's purpose-built for Autonomy's data model
- Not the final Autonomy Surface — it's the first iteration that teaches us what the Surface needs to be
