---
name: github
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
agents/capabilities/github/bin/declare-review-binding.sh
```

The helper resolves the current branch's PR via `gh pr view`, derives
the binding key automatically, and prefers the PR's live `baseRefOid`
so stacked PRs pointed at another branch get the right base commit
without hand-written SHAs.

If you need to override the base explicitly, or force chaining off the
previous PR's cached head SHA, use:

```bash
agents/capabilities/github/bin/declare-review-binding.sh \
  --previous-review-id <PREVIOUS_PR_NUMBER> \
  <THIS_PR_NUMBER>
```

The helper writes a ``autonomy.worktree.review_binding#1`` Setting
keyed ``<SESSION>:<REPO>:<BRANCH>:<PR>``. The dashboard reads the
binding, fetches PR state by id over REST, and stops scanning the
branch — no more rate-limit risk.

## After amending and pushing — declare the amend

Once the binding is in place, the canonical "I just amended a commit"
workflow is one call: refresh the dashboard's review snapshot **and**
arm a 2-hour terminal nag. Run after ``git push`` / ``git push
--force-with-lease``:

```bash
agents/capabilities/github/bin/declare-pr-amended.sh
```

This hits ``POST /api/worktrees/<SESSION>/<REPO>/refresh?nag_when_terminal=1``,
which:

* re-fetches PR + check-runs state and writes the cache (same as a
  manual Refresh click);
* arms the row's ``nag_when_terminal`` watch for 2 hours;
* schedules smart-cadence background polls (30s / 60s / 5min tiers)
  until each PR transitions to terminal or the 2-hour cap elapses.

When **any** PR's checks reach terminal (no ``running``/``pending``
checks remain), the dashboard sends one CrossTalk into the session:

```
You got merged review status: PR #303 — GREEN
All 4 checks passed.
```

…or, on red:

```
You got merged review status: PR #303 — RED
2 of 4 checks failed: build, lint
```

Stacked PRs each fire their own message. A subsequent push with a new
head_sha re-arms that PR's notification within the 2-hour window. If
nothing terminalizes by the cap, the watch silently disarms — never
an unbounded poll.

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
