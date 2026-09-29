---
name: autonomy/mermaid
description: Render Mermaid source to SVG and share inline tiles in the session viewer. Includes a hand-laid HTML escape hatch for diagrams Mermaid auto-layout can't handle cleanly.
---

# Mermaid capability

This workspace has `mermaid-share` and `mermaid-render` on `PATH`. They
take Mermaid source and produce SVG, optionally sharing it as a tile in
the dashboard session viewer in one tool call.

The renderer runs Mermaid via `puppeteer-core` driving the
agent-browser Chromium binary. Nothing fetches over the network at
render time — the capability is offline-safe.

## The fast path: `mermaid-share`

```bash
# stdin, with alt text
echo 'flowchart LR; A --> B --> C' | mermaid-share --alt "three-node demo"

# from a file, with caption
mermaid-share --in /tmp/diagram.mmd --caption "Workspace primer pipeline"

# multiline source via heredoc
mermaid-share --alt "settings → render → output" <<'EOF'
flowchart TB
  S1[autonomy.workspace#1]:::setting --> W[WorkspaceV1]:::step
  S2[autonomy.org#1]:::setting --> W
  W --> R[render_workspace_primer]:::step
  R --> O["~/.claude/CLAUDE.md"]:::out
EOF
```

**Constraints (same as `graph share`)**:
- One share per Bash tool result. The harness up-converts a single JSON
  object whose `type` is `viewer_attachment`; chaining (`mermaid-share
  ... && mermaid-share ...`) concatenates two JSON blobs and *neither*
  renders. Use one Bash invocation per share.
- Don't print other stdout in the same Bash call — extra output breaks
  the parser.

## Render-only: `mermaid-render`

```bash
mermaid-render --in diagram.mmd --out diagram.svg
mermaid-render < diagram.mmd > /tmp/x.svg     # stdin → stdout
mermaid-render --probe                          # health check; exit 0 = ok
```

Use when you want the SVG file without sharing — e.g. embedding in a
note, post-processing, or rendering many diagrams in a loop and sharing
selectively.

## Palette doctrine

Apply class names; the renderer injects `classDef` lines automatically
when the source doesn't already declare them:

| Class | Background | Border | Foreground | Use for |
|---|---|---|---|---|
| `:::setting` | `#dceaff` | `#1f4ea8` | `#0b2545` | graph Setting / config row |
| `:::file` | `#fde9c2` | `#a86a17` | `#3a2400` | file in repo |
| `:::step` | `#ffffff` | `#222222` | `#111111` | code path / process step |
| `:::out` | `#d8f0d8` | `#1f7a2a` | `#0b3a0b` | output / success terminal |
| `:::err` | `#fcd5d3` | `#982020` | `#400000` | error / fail path |
| `:::fall` | `#fbe9d7` | `#a06223` | `#3a1a00` | fallback / degraded |

If the source includes any `classDef` line, automatic injection is
skipped — you take over styling for the whole diagram.

## When Mermaid auto-layout fails: hand-laid HTML

Mermaid's flowchart auto-layout handles linear pipelines, simple DAGs,
sequence diagrams, and class diagrams cleanly. **Rule of thumb**: if
more than four input edges converge on a single node, the auto-layout
will crisscross the arrows and the visual hierarchy collapses. Switch
to a hand-laid HTML scaffold rendered via `agent-browser`.

The pattern: write HTML directly with the same palette, open in
`agent-browser`, full-page screenshot, share via `graph share`.

```bash
cat > /tmp/diagram.html <<'EOF'
<!doctype html>
<html><head><style>
  body { margin: 0; padding: 28px 36px; background: #fff;
         font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif; color: #111; }
  .row { display: grid; grid-template-columns: 380px 36px 1fr;
         align-items: start; column-gap: 14px; margin-bottom: 8px; }
  .step { background: #fff; border: 1.5px solid #222; border-radius: 8px;
          padding: 12px 14px; }
  .step .title { font-weight: 600; font-size: 14px; }
  .step .where { font-family: monospace; font-size: 12px; color: #666; margin-top: 2px; }
  .arrow { color: #555; font-size: 22px; text-align: center; padding-top: 18px; }
  .chip { display: inline-block; font-size: 12px; padding: 3px 9px;
          border-radius: 4px; margin: 0 6px 6px 0; border: 1px solid; }
  .chip.setting { background: #dceaff; border-color: #1f4ea8; color: #0b2545; }
  .chip.file    { background: #fde9c2; border-color: #a86a17; color: #3a2400; }
  .chip.out     { background: #d8f0d8; border-color: #1f7a2a; color: #0b3a0b; }
</style></head><body>
  <div class="row">
    <div class="step">
      <div class="title">1 · Build something</div>
      <div class="where">where it lives</div>
    </div>
    <div class="arrow">←</div>
    <div>
      <span class="chip setting">setting input</span>
      <span class="chip file">file input</span>
    </div>
  </div>
  <div class="arrow">↓</div>
  <!-- repeat for each step; outputs use class="step" with extra styling -->
</body></html>
EOF
agent-browser open file:///tmp/diagram.html --wait 1500
agent-browser screenshot --full /tmp/diagram.png
graph share /tmp/diagram.png --alt "..." --caption "..."
```

The hand-laid path produces a PNG (because agent-browser screenshots
are PNG). The Mermaid path produces SVG. Use SVG when Mermaid can do
the job; reach for PNG only when you've decided the diagram needs the
hand-laid layout.

## Probe / health check

```bash
mermaid-share --probe   # exit 0 if renderer is healthy, non-zero otherwise
mermaid-render --probe  # same; useful in scripts that need the renderer
```

The probe renders a trivial `flowchart LR; A --> B` end-to-end. Failure
modes:
- `tool_missing` — the script isn't on `PATH`. Capability not enabled
  on this workspace, or the package didn't mount.
- `env_missing: MERMAID_CHROMIUM` — no Chromium binary reachable. Set
  `MERMAID_CHROMIUM` to a chrome/chromium path.
- `probe_failed` — renderer raised. Stderr carries the error message.

## Mermaid syntax pointers

Common diagram types:

```
flowchart LR              # left-to-right boxes-and-arrows
flowchart TB              # top-to-bottom boxes-and-arrows
sequenceDiagram           # actor sequence with messages
classDiagram              # OOP class relationships
stateDiagram-v2           # state machine
gantt                     # timeline
erDiagram                 # entity-relationship
```

For full Mermaid syntax: <https://mermaid.js.org>

## Architecture reference

For the full architectural model — schema chain, package_root layout,
how the workspace primer surfaces this capability — read
`graph://47b55d8d-491` (canonical signpost).
