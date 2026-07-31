---
name: present
description: Create, publish, activate, watch, revise, and pull down presentations in Autonomy's Present app. Use for slide decks, reports, boards, dossiers, microsites, or other HTML artifacts intended for /present, and whenever updating an existing Present artifact.
---

# Present

Present currently displays Design Studio designs; there is no `graph present`
command. A Present library entry is keyed by the stable Design Studio
`design_id` and automatically hydrates its latest revision.

## Check before publishing

Search both catalogs before creating a presentation:

```bash
curl -sk 'https://localhost:8080/api/design-studio/designs?limit=500&sort=updated'
curl -sk https://localhost:8080/api/presentations/decks
```

If the exact title exists, update that stable design. Do not create and
activate a second design. Exact-name conflicts are rejected; use `--force`
only for an intentional duplicate.

## Create and publish a presentation

Put one complete HTML artifact in a directory, then run:

```bash
graph ui-design "Exact presentation title" /path/to/artifact --present
```

This publishes to Design Studio, activates the stable design in Present, and
keeps watching the directory. Use `--once` when no live watch is wanted. Open
the library at `/present` or the artifact at `/present/<design_id>`.

## Update an existing presentation

Load the `design-studio` skill, pull the latest revision down, edit it, and
publish with the stable ID:

```bash
graph ui-design "Exact presentation title" /path/to/artifact --design <design_id>
```

Do not pass `--present` again: the existing Present entry already follows the
latest Design Studio revision. Preserve the title unless intentionally
renaming the presentation.

## Activate an existing Design Studio design

Activation is an explicit write:

```bash
curl -sk -X POST \
  https://localhost:8080/api/presentations/deck/<design_id>/shown
```

Merely opening `GET /api/presentations/deck/<design_id>` does not add it to the
library. Activation returns HTTP 409 when another stable design has the exact
same presentation name. Append a revision to the existing design, or append
`?force=true` only when the second entry is intentional.

## Current artifact shape

Use one self-contained responsive HTML document. Present discovers slides from
explicit slide markers such as `[data-slide]`, `.slide`, `.present-slide`,
`section`, or `article`. The source of truth remains the Design Studio revision;
the Present record stores the stable pointer and display metadata.
