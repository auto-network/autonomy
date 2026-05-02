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

## After creating a PR — declare the binding

The Worktrees dashboard previously auto-detected your PR by scanning
``gh pr list --head <branch>`` every 30s. That ran into rate limits,
ghost-commits on squash-merge, and stacked-PR ambiguity. The new
flow is **operator/agent declaration**: tell the dashboard which review
covers which commit range, and it fetches state by id instead.

After you create the PR, run:

```bash
agents/capabilities/github/bin/declare-review-binding.sh <PR_NUMBER>
```

…or for stacked PRs (the second one onward), chain off the previous
PR's head SHA:

```bash
agents/capabilities/github/bin/declare-review-binding.sh \
  --previous-review-id <PREVIOUS_PR_NUMBER> \
  <THIS_PR_NUMBER>
```

The helper writes a ``autonomy.worktree.review_binding#1`` Setting
keyed ``<SESSION>:<REPO>:<BRANCH>:<PR>``. The dashboard reads the
binding, fetches PR state by id over REST, and stops scanning the
branch — no more rate-limit risk.

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
