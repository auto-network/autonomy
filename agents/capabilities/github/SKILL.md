# GitHub capability — agent skill (placeholder)

This file is a stable stub so future beads can target `agents/capabilities/github/SKILL.md`
without having to invent a path. It is not yet a real Agent Skills bundle.

The full skill content is added by the GitHub capability MVP bead (see
graph://86e04207-a25 § Phase 2 — *GitHub capability MVP for Worktrees*).

## What this capability will provide

Implementation: `autonomy/github`.

Contracts implemented:

- `source_control@1`
- `change_review@1`
- `merge_gates@1`

Delivery mode: `image_baked` — the workspace image already carries `gh`
on PATH and `GH_TOKEN` is provisioned at workspace launch.

## Intended agent surface

The agent will be told it can:

- inspect branch and commit state via `gh` and `git`
- resolve a branch to a PR and read PR state
- read merge-gate snapshots (checks, approvals, mergeability)
- enable watch modes (`Silent`, `Nag All Changes`, `Nag When Done`)

The exact phrasing lands when the MVP bead lands.
