---
name: design-studio
description: Create, publish, watch, revise, pull down, and implement Autonomy Design Studio designs with graph ui-design and the dashboard APIs. Use whenever working with Design Studio, design or revision IDs, UI mockups/specs, HTML variants, fixtures, or operator design review.
---

# Design Studio

Use the stable `design_id` for the whole design and a `revision_id` for one
saved iteration. Creating without `--design` starts a new design; every later
iteration must pass the stable ID.

## Check before publishing

Search the Design Studio library before creating anything:

```bash
curl -sk 'https://localhost:8080/api/design-studio/designs?limit=500&sort=updated'
```

Match the intended title exactly. If it exists, pull it down and append a
revision. New exact-name designs are rejected. Use `--force` only when the
duplicate is deliberate, never as a convenience after a conflict.

## Create or watch a design

Put one complete, responsive HTML document in a directory, then run:

```bash
graph ui-design "Exact display title" /path/to/design-dir
```

The command publishes immediately and then watches by default. Keep that one
process alive while editing; every save becomes another revision of the new
stable design. Do not relaunch the watcher. Use `--once` for a one-shot push.

Current Studio renders only the last `.html` file. Use one HTML file plus a
fixture with short named `states` instead of multiple variant files. Bind the
root with `x-data="window.FIXTURE"`; keep all shared fixture data inside every
state. Build responsive behavior with Tailwind's `md:` breakpoint.

## Pull down and revise an existing design

1. Read the design series and note `latest_revision_id`:

   ```bash
   curl -sk https://localhost:8080/api/design-studio/designs/<design_id>
   ```

2. Fetch that revision, including HTML, fixture, and ordered revision IDs:

   ```bash
   curl -sk https://localhost:8080/api/design/<latest_revision_id>/full
   ```

3. Save each `variants[].html` as `<variants[].id>.html` in a working
   directory and save `fixture` when present.

4. Publish back to the same stable design, preserving the display title unless
   the design is intentionally being renamed:

   ```bash
   graph ui-design "Exact display title" /path/to/design-dir --design <design_id>
   ```

Never substitute a revision ID for the stable `design_id` when appending.

## Direct API

`POST /api/design` creates a revision. Send `design_id` to append; omit it only
for a genuinely new design. A new exact-name collision returns HTTP 409 with
the matching stable IDs. Boolean `"force": true` bypasses the guard.

## Implement an approved design

Fetch `/api/design/<revision_id>/full`, use the selected variant when one is
selected (otherwise the last variant), and integrate its HTML/CSS directly.
Replace fixture data with real component state; do not redesign from prose.
