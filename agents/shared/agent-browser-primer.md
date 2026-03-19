# Agent-Browser — Self-Validation Primer

You have `agent-browser` available for visually validating your dashboard changes.
It's a headless Chrome CLI purpose-built for AI agents (~200-500 tokens per snapshot vs thousands for Playwright MCP).

## Environment

- Headless Chrome, `--no-sandbox` pre-configured
- Dark color scheme, PNG screenshots to `/tmp/screenshots/`
- Dashboard runs at `https://localhost:8080` (self-signed TLS)
- Pass `--ignore-https-errors` on the `open` command only (not on subsequent commands, or you'll get warnings)
- Element refs (e.g. `[1]`, `[2]`) invalidate on navigation — re-snapshot after navigating

## Quick Validation Pattern

```bash
agent-browser open https://localhost:8080/dispatch --ignore-https-errors
agent-browser wait --load networkidle
agent-browser snapshot -i            # DOM snapshot with interactive element refs
# Check: real bead data? titles, IDs, links present?
agent-browser screenshot --annotate  # visual screenshot with element labels
agent-browser close
```

## What Success Looks Like

- Snapshot shows interactive elements with real bead data (titles, IDs, clickable links)
- No raw `${variable}` or `{{ jinja_var }}` — all templates rendered
- SSE-driven sections show live data, not static skeletons

## What Failure Looks Like

- **Empty page** → Alpine.js error, check browser console via `eval "document.querySelectorAll('.x-data').length"`
- **Static skeleton** → SSE not connected, look for `EventSource` errors
- **Raw `${variable}`** → Jinja template not rendered server-side
- **`<script>` in titles renders as HTML** → XSS vulnerability (see check below)

## XSS Check

If bead titles or descriptions could contain user input:
```bash
# Verify script tags render as escaped text, not executable HTML
agent-browser snapshot -i | grep -i "<script>"
# Should see &lt;script&gt; or no match — never raw <script> in DOM
```

## Before/After Comparison

```bash
# Pixel diff against a baseline screenshot
agent-browser screenshot --annotate
# ... make changes ...
agent-browser screenshot --annotate
agent-browser diff screenshot --baseline /tmp/screenshots/prev.png
```

## Diff After Interaction

```bash
agent-browser snapshot -i              # capture before
agent-browser click [3]                # interact with element ref [3]
agent-browser diff snapshot            # shows what changed in DOM
```

## Dashboard-Specific Gotchas

- **SPA navigation:** Some views still use `onclick="navigateTo()"` on `<div>`/`<tr>` instead of `<a>` tags. These won't appear in `snapshot -i`. Use `eval "navigateTo('/bead/auto-xxxx')"` or click via `[data-bead-id]` selector where available.
- **Scrollable content area:** The main content is in `<main class="flex-1 overflow-auto">`, not the window. `agent-browser scroll` scrolls the viewport, so use eval for content scrolling:
  ```bash
  agent-browser eval "document.querySelector('main').scrollTop = document.querySelector('main').scrollHeight"
  ```
- **Eval variable scoping:** Multiple `eval` calls share the same page context. Use IIFEs to avoid `const` redeclaration errors:
  ```bash
  agent-browser eval "(() => { const x = ...; return x; })()"
  ```

## Command Reference

| Command | What it does |
|---------|-------------|
| `open <url>` | Open URL in headless Chrome |
| `snapshot -i` | DOM snapshot with interactive element refs |
| `snapshot -i -C` | Compact snapshot (less whitespace) |
| `click [N]` | Click element by ref number |
| `get text [N]` | Get text content of element |
| `get url` | Get current page URL |
| `screenshot` | Take PNG screenshot |
| `screenshot --annotate` | Screenshot with element labels overlaid |
| `diff snapshot` | DOM diff against previous snapshot |
| `diff screenshot --baseline <path>` | Pixel diff against baseline image |
| `wait --load networkidle` | Wait for network to settle |
| `wait --text "string"` | Wait for text to appear |
| `eval "js expression"` | Execute JavaScript in page context |
| `close` | Close browser session |
