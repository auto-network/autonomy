# Evidence Capture

Machinery for the multi-user mission's proof obligation: drive a real
dashboard instance through a workflow's actual UI with agent-browser, capture
every step as a screenshot, and emit a capture directory in the
evidence-manifest format (provisional v1 — frozen by the mission controller;
the format is documented on the Test & Automation pillar screen, mission
eec0efa1, and in the manifest itself).

## Usage

```bash
python3 -m tools.evidence.capture tools/evidence/specs/<flow>.json \
    [--out /workspace/output/evidence] [--keep-open]
```

Output: `<out>/row-<NNN>/<capture_id>/` containing `manifest.json`,
`steps/NN-<slug>.png`, and `gallery.html` (static, relative refs, with a loud
banner when `instance.kind` is not `real` — a mock gallery must never be
mistakable for proof).

## Flow specs

A spec is JSON: register `row` number, `row_title`, `base_url`, `instance`
metadata, and `steps` — each step is a caption plus a list of agent-browser
argv fragments (`{base}` expands to `base_url`). After each step's actions,
one screenshot is taken. A failing action still gets its screenshot, is
recorded with `status: "failed"`, and stops the run with a non-zero exit —
a failure capture is evidence too.

## Rules inherited from the test-value methodology (graph 73bad14e-65e)

- No fixed sleeps: waits are agent-browser condition waits only
  (`wait --load networkidle`, `wait --text`, `wait <selector>`), which fail
  loudly on timeout.
- Reuse, don't reinvent: this is a thin runner over agent-browser
  (see agents/shared/dashboard/agent-browser-primer.md); it adds only the
  step/manifest/gallery discipline.
- Each capture runs in its own named agent-browser session so it cannot
  disturb, or be disturbed by, other browser automation in the container.

## Known constraint

The dashboard's app routes (`/`, `/beads`, …) sit behind the sign-in gate;
unauthenticated automation sees the unlock screen. Mission Control site routes
(`/missions/<id>`, pillar screens) are reachable without sign-in. Flows behind
the gate need an authenticated driving path or an isolated test instance —
tracked on the Test & Automation pillar (bead auto-vztz5).
