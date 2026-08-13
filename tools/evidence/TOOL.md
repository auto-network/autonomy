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

## Two-phase captures and mid-flow values

`{env:NAME}` in an action expands from the environment at run time — for
values that only exist mid-ceremony (an approval id, a minted link URL).
`--continue-capture <dir>` appends a spec's steps to an existing capture,
for flows whose later steps depend on values produced by the earlier ones.
`tools/evidence/row56_run.sh` is the reference orchestrator using both.

## Publishing (the permanent store)

Captured evidence lives as GRAPH ATTACHMENTS (mission controller ruling,
2026-08-13): `python3 -m tools.evidence.publish <capture-dir>` creates one
graph note carrying every step image as an attachment and writes the
attachment ids and note id back into `manifest.json`. Run it OUTSIDE any
isolated-instance environment so the note lands in the real org graph. The
capture directory itself is staging, not the record.

## Driving flows behind the sign-in gate

The dashboard's app routes sit behind the sign-in gate; Mission Control site
routes do not. The proven answer for gated flows is a fully isolated real
instance with a throwaway identity and org, so automation types a test
password that secures nothing:

- real `tools.dashboard.server` + real `tools.network.registry` (TLS via a
  self-signed RSA cert — Chromium rejects Ed25519 server certs; trust it by
  pointing `SSL_CERT_FILE` at it so the dashboard and its spawned connector
  verify normally);
- every store env var from `tools/data_paths.py::STORE_MANIFEST` pointed at a
  scratch root, plus `AUTONOMY_REFUSE_REAL_DATA_FALLBACK=1` so an unset store
  raises instead of touching real data;
- `AUTONOMY_NETWORK_REGISTRY_URL` pointed at the local registry;
- identity + org provisioned headlessly (personal identity Setting with an
  armored root, then `org_ops.create_org_with_identity`), a note created via
  `tools.graph.ops.create_note`, and `static/tailwind.css` built or copied in
  (it is a gitignored build artifact; a worktree serves a placeholder).

The row-56 capture (register row 56, the share-link publish ceremony) runs
end to end on this stack: gate → password unlock → approval sheet →
in-browser signing → tunnel start → minted link → the note rendering over
the encrypted channel in a cold browser.
