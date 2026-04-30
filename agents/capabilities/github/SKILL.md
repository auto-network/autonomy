---
name: autonomy/github
description: GitHub source-control capability. Inspect branches, read PRs and reviews, watch merge-gates from inside the workspace.
---

# GitHub capability

This workspace has the GitHub CLI (`gh`) on PATH and `GH_TOKEN` already
provisioned. You can drive any GitHub workflow without touching credentials.

## What's available

- `gh` CLI for any GitHub operation (PRs, issues, checks, review, repo admin)
- `git` against `origin` works without re-authenticating
- The Dashboard reaches into this container via `docker exec ... gh ...` to
  drive the typed `source_control` capability surface — keep auth state
  consistent (don't `gh auth logout`)

## Common operations

```bash
# Find the PR for the current branch
gh pr view --json number,url,title,state,headRefName,baseRefName

# Check status of the PR's merge-gates
gh pr checks

# List recent PRs
gh pr list --limit 10

# Read a specific PR
gh pr view <NUMBER> --json number,title,body,state,mergeStateStatus,statusCheckRollup
```

## Deterministic surface (for reference)

The Dashboard does not call `gh` argv directly. It calls typed operations
under the `source_control@1` contract that this capability implements:

- `source_control.review.read` — PR review state for the row's branch
- `source_control.review.refresh` — re-fetch review state on demand
- `source_control.gates.watch_set` — set merge-gate watch mode
  (`subscribed` / `ignored` / `default`)

These map internally to fixed `gh` invocations against the live container
backing the Worktrees row. You shouldn't need to invoke them manually
from inside the session — they exist for Dashboard composition.

## Limitations

- Per-PR notification subscription is not yet supported by `gh` natively;
  watch is implemented at the *repository* subscription level. PR-thread
  subscription is on the follow-up list.
- The capability assumes one repo remote per worktree row. Forks / multi-remote
  configurations need explicit `--repo owner/repo` overrides.

## When to use raw `gh` vs. the deterministic surface

- **Raw `gh`:** anything interactive or experimental from inside the session.
- **Deterministic surface:** anything Dashboard / Worktrees needs to render.
  Don't shell out from Dashboard code; import from
  `agents.capabilities.github.service`.
