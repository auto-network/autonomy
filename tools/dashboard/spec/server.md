# Server Skeleton Specification

**Bead:** auto-5kj
**Priority:** P0
**Blocks:** Bead Board, Session Monitor, Knowledge Explorer, Markdown Viewer

## Purpose

Minimal Starlette server that serves the dashboard. Provides:
- Static file serving (JS, CSS)
- JSON API endpoints that shell out to `bd` and `graph` CLI tools
- Base HTML layout with navigation sidebar
- Client-side markdown rendering via marked.js

## Endpoints

### Pages (return HTML)
```
GET /                    → redirect to /beads
GET /beads               → Bead Board page
GET /sessions            → Session Monitor page
GET /search              → Knowledge Explorer page
GET /source/{id}         → Source Reader page
GET /bead/{id}           → Bead Detail page
```

### API (return JSON)
```
GET /api/beads/ready     → bd ready --json
GET /api/beads/list      → bd list --json
GET /api/beads/{id}      → bd show {id} --json
GET /api/beads/tree/{id} → bd dep tree {id} --json

GET /api/search?q=...&project=...&or=1&limit=N
                         → graph search "{q}" [--project ...] [--or] [--limit N]

GET /api/sources?project=...&type=...&limit=N
                         → graph sources [--project ...] [--type ...] [--limit N]

GET /api/source/{id}     → graph read {id} (full source, no cap by default)
GET /api/context/{id}/{turn}?window=3
                         → graph context {id} {turn} --window 3

GET /api/projects        → workspace registry (agents/projects.yaml)
GET /api/stats           → graph stats
```

### Source-read cap policy

Per-route ``max_chars`` rules — content caps on full-source reads:

| Route | Default cap | ``?max_chars=`` honoured? | Rationale |
|-------|-------------|---------------------------|-----------|
| ``GET /api/graph/{id}`` | unbounded (full source) | **no** — query param ignored | Browser surface (source viewer). Returns the full transcript so header metadata and the entries list stay consistent. The HTTP wire (``HttpClient.read_source_full``) does not carry ``max_chars`` either; the parameter is dead end-to-end on this route by design. |
| ``GET /api/source/{id}`` | unbounded (``max_chars=0``) | **yes** — caller may pass an explicit slice | Asset / full-read surface. Sole frontend caller wants the whole source; explicit override exists for callers that need a slice. |
| ``_resolve_primer`` (internal helper, not a route) | ``max_chars=50000`` | n/a — direct ``ops.read_source_full`` call | Cap protects the LLM context window — primer text is injected verbatim into the next session's first message. |

If you add a new caller that feeds a graph source into an LLM prompt, cap explicitly at the call site or use ``ops.read_source_full(..., max_chars=N)`` directly. Don't reintroduce the cap on the HTTP route — the only argument for it (defensive against accidental dumps) is outweighed by the present harm of silent metadata corruption when long sessions are silently truncated.

### Implementation Pattern

Every API endpoint follows the same pattern:
```python
async def api_beads_ready(request):
    result = await run_cli(["bd", "ready", "--json"])
    return JSONResponse(json.loads(result.stdout))

async def run_cli(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout.decode(), stderr.decode())
```

No direct database access. The CLI is the API.

## Base Layout

Single HTML template with:
- **Sidebar:** Nav links (Beads, Sessions, Search, Projects), graph stats summary
- **Main content area:** Rendered by page-specific JS
- **Search bar:** Global search input in the header, always visible
- **Scope indicator:** Current project scope (if any) shown in header

```html
<!DOCTYPE html>
<html>
<head>
  <title>Autonomy Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release/build/highlight.min.js"></script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release/build/styles/github-dark.min.css">
</head>
<body class="bg-gray-900 text-gray-100">
  <div class="flex h-screen">
    <!-- Sidebar -->
    <nav id="sidebar" class="w-56 bg-gray-800 p-4 flex flex-col">
      <!-- Nav links, stats summary -->
    </nav>
    <!-- Main -->
    <main id="content" class="flex-1 overflow-auto p-6">
      <!-- Page content rendered here -->
    </main>
  </div>
  <script src="/static/app.js"></script>
</body>
</html>
```

## Markdown Rendering

All markdown content rendered client-side via marked.js:
```javascript
function renderMarkdown(md) {
  const html = marked.parse(md);
  // Post-process: highlight code blocks
  el.innerHTML = html;
  el.querySelectorAll('pre code').forEach(block => hljs.highlightElement(block));
}
```

## File Layout

```
tools/dashboard/
├── README.md              ← vision doc (this exists)
├── spec/                  ← specifications per component
│   ├── server.md          ← this file
│   ├── bead-board.md      ← (next)
│   └── ...
├── server.py              ← Starlette app + routes
├── static/
│   ├── app.js             ← main JS (page routing, API calls, rendering)
│   └── style.css          ← any custom CSS beyond Tailwind
└── templates/
    └── base.html           ← base layout template
```

## Acceptance Criteria

- [ ] `python -m tools.dashboard` starts server on localhost:8080
- [ ] `/` redirects to `/beads`
- [ ] `/api/beads/ready` returns JSON from `bd ready --json`
- [ ] `/api/search?q=sovereignty` returns JSON from `graph search`
- [ ] `/api/source/{id}` returns full source content
- [ ] `/api/projects` returns project list
- [ ] Base layout renders with sidebar navigation
- [ ] Markdown content renders with syntax highlighting
- [ ] Navigation between pages works without full page reload
- [ ] Server starts in < 2 seconds

## Dependencies

Already in .venv:
- `starlette`
- `uvicorn`

No new dependencies required. CDN for Tailwind, marked.js, highlight.js.
