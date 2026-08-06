# Capability packages

This directory holds the repo-local package roots for capability
implementations (see graph://86e04207-a25 — *Workspace Capability Layer*).

A capability has two graph-backed schemas:

- `autonomy.capability.contract#1` — the provider-agnostic interface
- `autonomy.capability.impl#1` — a concrete implementation of one or more
  contracts

The `autonomy.capability.impl#1` Setting carries a `package_root` that
points into this tree (e.g. `agents/capabilities/github`). That root is
where the implementation's tools, skill projection, and primer projection
live. The graph schema is the *declaration*; this directory is the
*content* that the runtime materializes into a workspace.

## Layout

Each implementation gets its own subdirectory:

```
agents/capabilities/
├── README.md              ← this file
├── github/
│   ├── manifest.json      ← implementation metadata stub
│   ├── SKILL.md           ← agentic projection (Agent Skills format)
│   └── primer.md          ← primer projection
└── jira/
    ├── manifest.json
    ├── SKILL.md
    ├── primer.md
    └── tools/             ← shipped tool bundle (mounted_tools delivery)
```

`manifest.json` is a placeholder for now. It will eventually carry the
canonical reference back to the `autonomy.capability.impl#1` Setting and
any per-implementation metadata that does not belong in the Setting
itself (e.g. file checksums for the mounted tool bundle).

`SKILL.md` follows the Claude/Agent Skills format. It is the same
content the runtime projects into native skill bundles when the harness
supports them.

`primer.md` is the short fallback projection used by harnesses without
native skill support (see `agents/primer_renderer.py`).

The checked-in `SKILL.md` and `primer.md` are the portable, code-owned base.
Provider-instance guidance does not belong in this public package: an
organization adds it through `autonomy.org.capability.primer#1`, keyed by the
implementation id (for example `autonomy/jira`) with optional named blocks
such as `autonomy/jira:workflow`. At session launch those blocks are appended
to both the rendered workspace capability primer and the session-local skill
copy; the package files themselves are never rewritten.

## Scope

This bead establishes the directory layout and stable placeholder files
only. Populating real implementation logic for GitHub and Jira happens in
follow-up beads:

- `auto-uqq0i` — runtime materialization of enabled workspace capabilities
- (planned) GitHub Worktrees integration via `autonomy/github`
- (planned) Jira tooling productization via `autonomy/jira`

Until those land, the only contract here is the *path layout* — schemas
and downstream code can rely on these roots existing without committing
to their internal shape.

## Versioning

Capability implementations follow the versioning model documented in the
schema modules: monotonic integer `version`, `name@N` is canonical-pinned,
unsuffixed names refer to the unpinned working version. Edits within the
working version do not advance the version. The release/pin workflow is
not yet implemented.
