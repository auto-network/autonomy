# Worktrees Dashboard Specification

## Purpose

The Worktrees dashboard is the host-side review and action surface for session
worktrees under `data/worktrees/`. Its job is not to expose raw git state in
the abstract. Its job is to surface session output in the unit that matters for
integration:

- committed work first
- uncommitted changes second
- explicit host-side actions
- predictable ordering and safety rules

The page is intentionally commit-first. Dirty work is visible and reviewable,
but it is not the primary integration workflow.

## Implementation Milestones

Key commits in the current feature history:

- `c017c11` — initial scanner, monitor, routes, and `/worktrees` shell
- `4c845fc` — commit-first dashboard rewrite and autonomy-only merge hardening
- `1ce4591` — filter already-merged commits out of the pending review queue
- `a8a37ff` — clone-stale detection, sync-base flow, and request-rebase UX
- `172e7d6` — keep rebase requests visible for dirty worktrees
- `0455227` — syntax-highlighted diff rendering
- `c4a9cbc` — sticky review headers fix
- `93b9698` — dirty work no longer blocks autonomy merges; refresh fallback hardened
- `83e38d1` — proper fix for the empty worktrees load state; watchdog removed
- `0eca483` — L2.B behavioral sweep coverage for Worktrees via `DASHBOARD_MOCK`

## Mental Model

Three git locations matter, and the feature depends on keeping them distinct:

1. Managed clone
- Host path under `data/repos/...`
- Backing repo for session worktrees
- Holds the branch refs a session worktree uses

2. Session worktree
- Host path under `data/worktrees/{session}/{repo}`
- Mounted into the container as the writable checkout at `/workspace/repo`
- Usually starts on `session/{session_name}`
- Can have commits, dirty files, or both

3. Host integration checkout
- `REPO_ROOT` in the dashboard process
- The checkout the host uses when applying autonomy merges
- Distinct from both the managed clone and the session worktree

The merge path is therefore not “merge the worktree in place”. The dashboard
fetches from the managed clone into the host integration checkout and then
advances the target branch there if the selected commit is eligible.

## Current Backend Architecture

### Workspace Model

`agents/workspace_manager.py` provides the dashboard-facing model:

- `GitFileChange`
- `WorktreeCommit`
- `WorktreeDirtyDetail`
- `WorktreeState`

These structures capture:

- session/repo identity
- branch and target-branch label
- commit stack ahead of the dashboard base
- dirty files
- clone staleness
- rebase-required state
- session liveness
- file stats and patch data

### Scanner

`scan_all_worktrees()` walks `data/worktrees/` and returns one
`WorktreeState` per `(session, repo)`.

For each worktree it derives:

- `branch`
- `commits_ahead`
- `is_dirty`
- `ff_eligible`
- `clone_stale`
- `rebase_required`
- `session_live`
- `managed_clone`
- `commits`
- `dirty_files`

Important current behavior:

- the scan uses `base_ref..HEAD`
- for autonomy, the dashboard base can advance to the live host integration
  branch if that target head is an ancestor of the worktree head
- commit stacks are filtered to dashboard-pending commits, so already-merged
  SHAs are skipped even if `commits_ahead` remains non-zero due to an older
  fork point
- patch bodies are deferred; the 30s background scan reads metadata, name
  status, and numstat, but not full patches

### Monitor

`tools/dashboard/worktree_monitor.py` mirrors the session monitor pattern:

- 30s async background polling
- cached `list[WorktreeState]`
- `get_all()`
- `refresh()`

### API Surface

`tools/dashboard/server.py` exposes:

- `GET /api/worktrees`
- `POST /api/worktrees/refresh`
- `GET /api/worktrees/{session}/{repo}/commits/{sha}`
- `GET /api/worktrees/{session}/{repo}/changes`
- `POST /api/worktrees/{session}/{repo}/commits/{sha}/merge`
- `POST /api/worktrees/{session}/{repo}/sync-base`
- `POST /api/worktrees/{session}/{repo}/request-rebase`
- `POST /api/worktrees/{session}/{repo}/merge`
- `POST /api/worktrees/{session}/{repo}/discard`
- `POST /api/worktrees/{session}/cleanup`

The sidebar nav also consumes dual counts from the cached monitor:

- worktrees with commits
- worktrees with uncommitted changes

## Current UI Surface

### Top-Level Page

`/worktrees` currently ships as:

- page title `Worktrees`
- compact updated timestamp + refresh control
- three summary tiles:
  - `Commits (All Repos)`
  - `Commits (Autonomy)`
  - `Uncommitted Changes`
- two top-level modes:
  - `Commits`
  - `Changes`

### Commit Mode

Commit mode is the default queue.

Each worktree with pending commits renders as one visible card representing the
next mergeable/reviewable commit in that worktree stack:

- org badge
- session badge
- status badge
- `commit 1 of N`
- `Review`
- `src` / `dst` branch chips
- subject + body preview
- flattened file list
- note when later commits remain in the stack

The list shows only the oldest pending commit for each worktree. Later commits
are visible only after opening review.

### Commit Review

Commit review is a full-screen overlay, not an inset modal.

It includes:

- short SHA/session/timestamp badges
- integrated commit pager
- close `X`
- `src` / `dst` chips above the title
- markdown-rendered commit body using the shared dashboard markdown path
- sticky title on mobile with compact-on-pin behavior
- sticky `Files in this commit` row
- sticky per-file header row while diff is expanded
- full-width diff rendering with horizontal scroll only inside the diff area
- syntax-highlighted diff foreground via vendored local `highlight.js`
- add/delete line backgrounds preserved behind syntax-colored code

Merge success flow:

- spinner in button
- green check
- visible burst/confetti
- row poof
- advance in place to the next pending commit if one remains
- otherwise close back to the list

### Changes Mode

Changes mode is secondary and review-only.

Dirty cards show:

- org badge
- session badge
- repo badge
- status badge
- session title when present
- flat file list
- `View Diffs`
- `Discard` only for non-live worktrees

Dirty review uses the same sticky/pinned review layout as commit review, but
has no merge action.

## Current Merge Semantics

### Per-Commit, Ordered

All dashboard merges are per-commit.

The backend enforces:

- the requested SHA must still be in the pending dashboard list
- only `pending[0]` is mergeable

The UI mirrors that:

- the main queue shows only the oldest pending commit
- later commits in review disable merge with `Merge earlier commit first`

### Three Action States

The current autonomy flow has three distinct states:

1. Clone stale
- UI shows `Sync Worktree to Latest`
- no merge, no rebase request yet

2. Clone current and ff-eligible
- UI shows enabled `Merge ... into <branch>`

3. Clone current but not ff-eligible
- merge is disabled with an inline visible reason
- `Request Rebase` is shown for live sessions when rebase is the correct next
  action

### Autonomy-Only Integration

Today only the `autonomy` repo has a supported merge target.

For autonomy:

- the target repo is the host integration checkout at `REPO_ROOT`
- the target branch is explicit, not “whatever happens to be checked out”
- the merge fetches from the managed clone, then fast-forwards the target
  branch in the host repo
- after success, the managed clone’s target branch ref is synced back from the
  host repo so the next scan sees the correct base

Non-autonomy repos are currently review-only.

### Dirty Files Do Not Block Merge

Dirty state is informational for the dashboard and Changes view. It does not
block an autonomy merge, because the actual merge happens in the host
integration checkout, not inside the session worktree.

## Current Rebase Flow

When the target branch has advanced beyond the worktree fork point:

- merge returns structured `rebase_required`
- the UI opens a dialog instead of only toasting
- `Request Rebase` sends CrossTalk as `Dashboard UI`
- before sending, the endpoint syncs the managed clone target branch from the
  host repo
- the session is instructed to run only `git rebase <target_branch>`

If the worktree also has dirty files, the rebase request remains visible. The
message explains that the session may need to stash or commit dirty work before
rebasing.

## Recovery / Broken-State Handling

The original empty `/worktrees` load issue came from a style-first fragment:

- the fragment began with a top-level `<style>`
- SPA routing initialized Alpine on `firstElementChild`
- Alpine never reached the sibling `x-data="worktreesPage()"`

The current fix is structural, not watchdog-based:

- style-first fragments are wrapped in a single root
- the worktrees watchdog/latch was removed
- the refresh control now also has a hard-reload fallback if Alpine fails to
  intercept the click

## Testing

### L2.A / HTTP + Wiring

`tools/dashboard/tests/test_worktrees.py` covers:

- endpoint JSON shape
- refresh behavior
- merge/sync/rebase/discard/cleanup wiring
- template and route wiring
- worktrees shell/fragment rendering assumptions

### L2.B / Behavioral Sweep

`tools/dashboard/tests/test_behavioral_sweep.py` now includes a dedicated
Worktrees page sweep backed by `DASHBOARD_MOCK` fixture data.

It proves:

- page mount with real data
- summary tiles render expected counts
- commit review opens and stays open
- Changes mode renders dirty cards
- live dirty cards do not show visible `Discard`
- dirty review opens and stays open

Measured incremental cost of the Worktrees L2.B addition against the full
behavioral sweep:

- baseline full sweep: `47.865s`
- with Worktrees L2.B: `52.679s`
- incremental wall-clock cost: about `4.8s`

## Current Limitations

1. Repo-specific merge/application is not implemented
- non-autonomy repos are still review-only

2. Branch-target intent is still mostly policy-driven
- autonomy uses explicit target-branch policy
- future multi-repo support needs first-class `source_branch` /
  `target_branch` semantics

3. Rebase/mergeability is not yet a fully separate preflight contract
- the UI has the current state fields it needs
- a richer generic `apply_mode` / `blocked_reason` contract would still be an
  improvement

4. Dirty untracked files still do not render inline patch bodies
- they are listed individually now
- but fully new files do not yet show full synthetic diff bodies

## Open Deliverables

Near-term remaining work:

- make source-branch to target-branch intent explicit for non-autonomy repos
- add richer preflight fields for apply mode and blocked reason
- decide whether “already applied but amended” detection needs patch-id style
  matching
- expand L2.B coverage if Worktrees grows more action states

This spec is intended to describe the actual current Worktrees surface, not the
original narrow bead scope.
