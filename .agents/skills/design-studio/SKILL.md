---
name: design-studio
description: Create, publish, watch, revise, pull down, and implement Autonomy Design Studio designs with graph ui-design and the dashboard APIs. Use whenever working with Design Studio, design or revision IDs, UI mockups/specs, HTML variants, fixtures, or operator design review.
---

# Design Studio

**Prime directive:** A Design Studio experiment is the production UI verbatim—simulate production inputs and view-state transitions behind the scenes, but include no non-production chrome, borders, annotations, labels, controls, or explanatory elements, and make every visible control fully functional against the same state shape the production API will provide.

Use the stable `design_id` for the whole design and a `revision_id` for one
saved iteration. Creating without `--design` starts a new design; every later
iteration must pass the stable ID.

## Check before publishing

Search the Design Studio library before creating anything:

```bash
graph ui-design --list
```

Pass an optional query to narrow the listing, for example
`graph ui-design --list "Exact display title"`. The CLI authenticates with the
container session's CrossTalk token and the dashboard returns only that org's
designs. Match the intended title exactly. If it exists, pull it down and
append a revision. New exact-name designs are rejected. Use `--force` only
when the duplicate is deliberate, never as a convenience after a conflict.

## Create or watch a design

Put one complete, responsive HTML document in a directory, then run:

```bash
graph ui-design "Exact display title" /path/to/design-dir
```

The command publishes immediately and then watches by default. Keep that one
process alive while editing; every save becomes another revision of the new
stable design. Do not relaunch the watcher. Use `--once` for a one-shot push.

Current Studio renders only the last `.html` file. Use one HTML file and bind
the root with `x-data="window.FIXTURE"`. Prefer one production-shaped fixture
whose invisible script drives state transitions through the same data shape
the real API will provide. Use named fixture `states` only to select divergent
starting conditions that one production participant cannot naturally reach
from another, such as the opposite sides of one exchange, different accounts,
or incompatible device/permission conditions. The harness picker is never a
scene selector or a substitute for navigation: from each starting condition,
the user must reach every possible state through the production controls and
normal asynchronous transitions. Never add an in-design state switcher,
timer, simulator button, annotation, or status label. Build responsive
behavior with Tailwind's `md:` breakpoint.

## Pull down and revise an existing design

1. Pull the latest revision of the stable design into a working directory:

   ```bash
   graph ui-design --pull <design_id> /path/to/design-dir
   ```

   The CLI resolves the latest revision, writes each variant as
   `<variants[].id>.html`, writes `fixture.json` when present, and prints the
   exact append command.

2. Publish back to the same stable design, preserving the display title unless
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
Replace only the simulated input source with the real API; the visible DOM,
styles, interactions, transitions, responsive behavior, and state shape must
already be production-final and must not be redesigned from prose.
