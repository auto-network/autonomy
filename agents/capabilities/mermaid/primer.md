## Mermaid capability

`mermaid-share` is on `PATH`. Render Mermaid source to SVG and drop it
into the session viewer in one tool call:

```bash
echo 'flowchart LR
  A[setting]:::setting --> B[step]:::step --> C[output]:::out' \
  | mermaid-share --alt "two-step pipeline" --caption "demo"

mermaid-share --in /tmp/diagram.mmd --caption "Workspace primer pipeline"
```

Output is SVG — vector, ~10–50 KB for typical flowcharts, scales
perfectly. The dashboard tile renderer displays it inline.

**Palette** — apply class names; styling is automatic:

| Class | Use for |
|---|---|
| `:::setting` | graph Setting / config row (blue) |
| `:::file` | file in repo (amber) |
| `:::step` | code path / process step (white) |
| `:::out` | output / success terminal (green) |
| `:::err` | error / fail path (red) |
| `:::fall` | fallback / degraded (orange) |

**Rules**:
- One share per Bash tool result. Don't print other stdout in the same
  call (chained `&&` with anything else breaks the parser).
- More than 4 input edges converging on one node? Mermaid's auto-layout
  tangles. Switch to the hand-laid HTML escape hatch in SKILL.md.
- `mermaid-render` is the render-only sibling (writes SVG to file/stdout
  without sharing).
- `mermaid-share --probe` exits 0 if the renderer is healthy.

For the long-form skill (auto-layout vs hand-laid decision tree, full
palette specification, the hand-laid HTML scaffold), run:

```bash
graph capability primer --app autonomy/mermaid --extended
```

See `SKILL.md` in the package root.
