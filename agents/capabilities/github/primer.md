## GitHub capability

`gh` is on PATH and `GH_TOKEN` is already configured — no auth setup
needed. Use it freely for PR inspection, checks, review, and any GitHub
operation.

Common one-liners:

- `gh pr view --json number,url,title,state` — PR for current branch
- `gh pr checks` — merge-gate status
- `gh pr list --limit 10` — recent PRs

The Dashboard composes typed `source_control@1` operations
(`review.read`, `review.refresh`, `gates.watch_set`) from
`agents/capabilities/github/service.py` against your live container —
don't `gh auth logout`. Per-PR watch is repo-level for now;
PR-thread subscription is a follow-up.

For full skill content see `agents/capabilities/github/SKILL.md`.
