"""HTTP and wiring tests for the Worktrees dashboard page."""

import asyncio
from pathlib import Path

import pytest

from agents.workspace_manager import (
    CleanupResult,
    GitFileChange,
    RebaseRequiredError,
    WorktreeCommit,
    WorktreeState,
    WorkspaceError,
)


JS_DIR = Path(__file__).resolve().parents[1] / "static" / "js"
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"


class _FakeMonitor:
    def __init__(self, rows):
        self.rows = rows
        self.refresh_count = 0

    def get_all(self):
        return list(self.rows)

    async def refresh(self):
        self.refresh_count += 1
        return list(self.rows)


def _row(
    session="auto-test",
    repo="autonomy",
    *,
    ahead=1,
    dirty=False,
    ff=True,
    clone_stale=False,
    rebase_required=False,
    live=False,
    commits=None,
    dirty_files=None,
):
    if commits is None and ahead:
        commits = [_commit()]
    return WorktreeState(
        session_name=session,
        repo_name=repo,
        worktree_path=Path("/tmp/worktrees") / session / repo,
        managed_clone=Path("/tmp/repos/autonomy.git"),
        branch=f"session/{session}",
        commits_ahead=ahead,
        is_dirty=dirty,
        ff_eligible=ff,
        clone_stale=clone_stale,
        rebase_required=rebase_required,
        session_live=live,
        commits=commits or [],
        dirty_files=dirty_files or [],
    )


def _commit(sha="abcdef1234567890", subject="Add worktree dashboard"):
    return WorktreeCommit(
        sha=sha,
        short_sha=sha[:7],
        subject=subject,
        author="agent",
        date="2026-04-23 02:00",
        body="Commit body",
        files=[
            GitFileChange(
                status="M",
                path="tools/dashboard/static/js/pages/worktrees.js",
                additions=12,
                deletions=3,
            ),
        ],
        patch="diff --git a/file b/file",
    )


def _install_fake_monitor(monkeypatch, rows):
    from tools.dashboard import server

    fake = _FakeMonitor(rows)
    monkeypatch.setattr(server.worktree_monitor, "get_all", fake.get_all)
    monkeypatch.setattr(server.worktree_monitor, "refresh", fake.refresh)
    return server, fake


class TestWorktreeAPI:
    def test_count_worktrees_counts_all_dirty_rows_for_changes_badge(self, monkeypatch):
        from tools.dashboard import server

        rows = [
            _row(session="auto-commit", ahead=1, dirty=True),
            _row(session="auto-dirty", ahead=0, dirty=True, commits=[]),
            _row(session="auto-clean", ahead=0, dirty=False, commits=[]),
        ]
        monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: list(rows))

        assert server._count_worktrees() == {"with_commits": 1, "with_changes": 2}

    def test_get_worktrees_serializes_cached_rows(self, test_client, monkeypatch):
        _server, _fake = _install_fake_monitor(monkeypatch, [_row()])

        resp = test_client.get("/api/worktrees")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["session_name"] == "auto-test"
        assert row["session_title"] == ""
        # ``session_project`` lets the review screen link the session
        # badge back to the page-mode session viewer for live rows.
        # Empty when no tmux_sessions row matches (the case here).
        assert row["session_project"] == ""
        assert row["repo_name"] == "autonomy"
        assert row["worktree_path"] == "/tmp/worktrees/auto-test/autonomy"
        assert row["managed_clone"] == "/tmp/repos/autonomy.git"
        assert row["branch"] == "session/auto-test"
        assert row["target_branch"] in {"main", "master"}
        assert row["commits_ahead"] == 1
        assert row["is_dirty"] is False
        assert row["ff_eligible"] is True
        assert row["clone_stale"] is False
        assert row["rebase_required"] is False
        assert row["session_live"] is False
        assert row["dirty_files"] == []
        assert row["commits"] == [{
            "sha": "abcdef1234567890",
            "short_sha": "abcdef1",
            "subject": "Add worktree dashboard",
            "author": "agent",
            "date": "2026-04-23 02:00",
            "body": "Commit body",
            "files": [{
                "status": "M",
                "path": "tools/dashboard/static/js/pages/worktrees.js",
                "additions": 12,
                "deletions": 3,
            }],
            "stats": {
                "files": 1,
                "additions": 12,
                "deletions": 3,
            },
        }]

    def test_post_worktrees_refresh_forces_live_rescan(self, test_client, monkeypatch):
        _server, fake = _install_fake_monitor(monkeypatch, [_row()])

        resp = test_client.post("/api/worktrees/refresh")

        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert fake.refresh_count == 1

    def test_sync_base_endpoint_refreshes_and_returns_updated_state(self, test_client, monkeypatch):
        server, fake = _install_fake_monitor(monkeypatch, [_row(clone_stale=False)])
        called = {}

        def fake_sync(session_name, repo_name):
            called["args"] = (session_name, repo_name)
            return {"target_branch": "master", "managed_clone": "/tmp/repos/autonomy.git"}

        monkeypatch.setattr(server, "sync_session_worktree_base", fake_sync)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/sync-base")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["state"]["clone_stale"] is False
        assert called["args"] == ("auto-test", "autonomy")
        assert fake.refresh_count == 1

    def test_merge_endpoint_fast_forwards_and_refreshes_cache(self, test_client, monkeypatch):
        server, fake = _install_fake_monitor(monkeypatch, [_row()])
        called = {}

        def fake_merge(session_name, repo_name):
            called["args"] = (session_name, repo_name)
            return {"commit": "abc1234", "message": "merged", "target_repo": "/repo"}

        monkeypatch.setattr(server, "merge_session_worktree", fake_merge)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/merge")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "commit": "abc1234", "message": "merged"}
        assert called["args"] == ("auto-test", "autonomy")
        assert fake.refresh_count == 1

    def test_merge_endpoint_returns_409_when_cached_row_not_ff_eligible(self, test_client, monkeypatch):
        server, _fake = _install_fake_monitor(
            monkeypatch,
            [_row(ahead=0, dirty=False, ff=False)],
        )
        called = {"merge": False}

        def fake_merge(_session_name, _repo_name):
            called["merge"] = True
            raise AssertionError("merge helper should not be called")

        monkeypatch.setattr(server, "merge_session_worktree", fake_merge)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/merge")

        assert resp.status_code == 409
        assert resp.json()["error"] == "worktree is not ff-eligible"
        assert called["merge"] is False

    def test_merge_endpoint_returns_404_for_missing_worktree(self, test_client, monkeypatch):
        _server, _fake = _install_fake_monitor(monkeypatch, [])

        resp = test_client.post("/api/worktrees/missing/autonomy/merge")

        assert resp.status_code == 404
        assert resp.json()["error"] == "worktree not found"

    def test_merge_endpoint_surfaces_workspace_errors_as_conflicts(self, test_client, monkeypatch):
        server, _fake = _install_fake_monitor(monkeypatch, [_row()])

        def fake_merge(_session_name, _repo_name):
            raise WorkspaceError("ff-only failed")

        monkeypatch.setattr(server, "merge_session_worktree", fake_merge)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/merge")

        assert resp.status_code == 409
        assert resp.json()["error"] == "ff-only failed"

    def test_commit_detail_endpoint_returns_patch(self, test_client, monkeypatch):
        server, _fake = _install_fake_monitor(monkeypatch, [_row()])

        def fake_detail(session_name, repo_name, sha):
            assert (session_name, repo_name, sha) == ("auto-test", "autonomy", "abcdef1")
            return _commit()

        monkeypatch.setattr(server, "get_session_worktree_commit_detail", fake_detail)

        resp = test_client.get("/api/worktrees/auto-test/autonomy/commits/abcdef1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["sha"] == "abcdef1234567890"
        assert data["patch"] == "diff --git a/file b/file"

    def test_changes_detail_endpoint_returns_patch(self, test_client, monkeypatch):
        server, _fake = _install_fake_monitor(
            monkeypatch,
            [_row(dirty=True, dirty_files=[GitFileChange(status="M", path="dirty.txt")])],
        )

        def fake_detail(session_name, repo_name):
            assert (session_name, repo_name) == ("auto-test", "autonomy")
            return server.WorktreeDirtyDetail(
                files=[GitFileChange(status="M", path="dirty.txt", additions=4, deletions=1)],
                patch="diff --git a/dirty.txt b/dirty.txt",
            )

        monkeypatch.setattr(server, "get_session_worktree_dirty_detail", fake_detail)

        resp = test_client.get("/api/worktrees/auto-test/autonomy/changes")

        assert resp.status_code == 200
        assert resp.json() == {
            "files": [{
                "status": "M",
                "path": "dirty.txt",
                "additions": 4,
                "deletions": 1,
            }],
            "patch": "diff --git a/dirty.txt b/dirty.txt",
        }

    def test_commit_merge_endpoint_merges_selected_sha_and_refreshes(self, test_client, monkeypatch):
        server, fake = _install_fake_monitor(monkeypatch, [_row()])
        called = {}

        def fake_merge(session_name, repo_name, sha):
            called["args"] = (session_name, repo_name, sha)
            return {"commit": "abcdef1234567890", "message": "Add worktree dashboard"}

        monkeypatch.setattr(server, "merge_session_worktree_commit", fake_merge)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/commits/abcdef1/merge")

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "commit": "abcdef1234567890",
            "message": "Add worktree dashboard",
        }
        assert called["args"] == ("auto-test", "autonomy", "abcdef1")
        assert fake.refresh_count == 1

    def test_commit_merge_endpoint_returns_structured_rebase_required_payload(self, test_client, monkeypatch):
        server, fake = _install_fake_monitor(monkeypatch, [_row(ff=False, live=True)])

        def fake_merge(_session_name, _repo_name, _sha):
            raise RebaseRequiredError(
                target_branch="master",
                commits_behind=3,
                fork_sha="2d10a47deadbeef",
                session_live=True,
            )

        monkeypatch.setattr(server, "merge_session_worktree_commit", fake_merge)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/commits/abcdef1/merge")

        assert resp.status_code == 409
        assert resp.json() == {
            "error": "rebase_required",
            "message": "Parent has advanced 3 commits, rebase required before merge.",
            "commits_behind": 3,
            "session_live": True,
            "target_branch": "master",
            "fork_sha": "2d10a47deadbeef",
        }
        assert fake.refresh_count == 0

    def test_request_rebase_endpoint_syncs_clone_then_sends_dashboard_ui_crosstalk(self, test_client, monkeypatch):
        server, fake = _install_fake_monitor(monkeypatch, [_row(ff=False, live=True)])
        called = {}

        def fake_info(session_name, repo_name, *, sync_managed_clone_target=False):
            called["info"] = (session_name, repo_name, sync_managed_clone_target)
            return {
                "target_branch": "master",
                "commits_behind": 2,
                "fork_sha": "2d10a47deadbeef",
                "session_live": True,
                "commit": "abcdef1234567890",
                "is_dirty": False,
            }

        async def fake_send(target_session, message):
            called["send"] = (target_session, message)

        monkeypatch.setattr(server, "get_session_worktree_rebase_info", fake_info)
        monkeypatch.setattr(server, "_send_dashboard_ui_crosstalk", fake_send)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/request-rebase")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "session": "auto-test"}
        assert called["info"] == ("auto-test", "autonomy", True)
        assert called["send"][0] == "auto-test"
        assert "Dashboard UI" not in called["send"][1]
        assert "git rebase master" in called["send"][1]
        assert fake.refresh_count == 1

    def test_cleanup_endpoint_calls_workspace_cleanup_and_refreshes(self, test_client, monkeypatch):
        server, fake = _install_fake_monitor(monkeypatch, [_row()])
        called = {}

        def fake_cleanup(session_name, *, force=False):
            called["args"] = (session_name, force)
            return CleanupResult(
                removed=["/tmp/worktrees/auto-test/autonomy"],
                preserved=[("/tmp/worktrees/auto-test/enterprise", "local commits")],
                errors=[],
            )

        monkeypatch.setattr(server, "cleanup_session_worktrees", fake_cleanup)

        resp = test_client.post("/api/worktrees/auto-test/cleanup", json={"force": True})

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "removed": ["/tmp/worktrees/auto-test/autonomy"],
            "preserved": [
                {
                    "path": "/tmp/worktrees/auto-test/enterprise",
                    "reason": "local commits",
                },
            ],
            "errors": [],
        }
        assert called["args"] == ("auto-test", True)
        assert fake.refresh_count == 1

    def test_discard_endpoint_calls_repo_cleanup_and_refreshes(self, test_client, monkeypatch):
        server, fake = _install_fake_monitor(monkeypatch, [_row(dirty=True)])
        called = {}

        def fake_cleanup(session_name, repo_name, *, force=False):
            called["args"] = (session_name, repo_name, force)
            return CleanupResult(
                removed=["/tmp/worktrees/auto-test/autonomy"],
                preserved=[],
                errors=[],
            )

        monkeypatch.setattr(server, "cleanup_session_worktree", fake_cleanup)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/discard")

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "removed": ["/tmp/worktrees/auto-test/autonomy"],
            "preserved": [],
            "errors": [],
        }
        assert called["args"] == ("auto-test", "autonomy", True)
        assert fake.refresh_count == 1

    def test_discard_endpoint_surfaces_live_worktree_rejection(self, test_client, monkeypatch):
        server, _fake = _install_fake_monitor(monkeypatch, [_row(dirty=True, live=True)])

        def fake_cleanup(_session_name, _repo_name, *, force=False):
            assert force is True
            raise WorkspaceError("cannot discard live worktree for session auto-test")

        monkeypatch.setattr(server, "cleanup_session_worktree", fake_cleanup)

        resp = test_client.post("/api/worktrees/auto-test/autonomy/discard")

        assert resp.status_code == 409
        assert resp.json()["error"] == "cannot discard live worktree for session auto-test"


class TestWorktreePage:
    def test_worktrees_page_shell_and_fragment_render(self, test_client):
        shell = test_client.get("/worktrees")
        assert shell.status_code == 200
        assert "/static/js/pages/worktrees.js" in shell.text
        assert "/static/vendor/highlightjs/highlight.min.js" in shell.text
        assert "/static/vendor/highlightjs/github-dark.min.css" in shell.text
        assert 'href="/worktrees"' in shell.text

        fragment = test_client.get("/pages/worktrees")
        assert fragment.status_code == 200
        html = fragment.text
        assert "worktreesPage()" in html
        assert 'data-testid="worktrees-table"' in html
        assert 'data-testid="review-commit-button"' in html
        assert 'data-testid="discard-dirty-button"' in html
        assert 'data-testid="worktree-commit-merge-button"' in html

    def test_static_js_wires_polling_and_actions(self):
        js = (JS_DIR / "pages" / "worktrees.js").read_text()
        assert "fetch('/api/worktrees')" in js
        assert "fetch('/api/worktrees/refresh', { method: 'POST' })" in js
        assert "_highlightDiffText(path, text)" in js
        assert "hljs.highlight(source, { language, ignoreIllegals: true })" in js
        assert "get uncommittedChangesCount()" in js
        assert "return this.rows.filter(row => row.is_dirty).length;" in js
        assert "if (item.row.is_dirty) return 'Uncommitted changes are present in this worktree';" not in js
        assert "'/api/worktrees/' + encodeURIComponent(row.session_name)" in js
        assert "'/sync-base'" in js
        assert "'/request-rebase'" in js
        assert "rebase_required" in js
        assert "canDiscardDirtyRow(row)" in js
        assert "fitPath(path, el)" in js
        assert "repoName(row)" in js
        assert "row.repo_name === 'autonomy'" in js
        assert "IntersectionObserver" in js
        assert "observeReviewTitleSentinel()" in js
        assert "'/commits/'" in js
        assert "'/merge'" in js
        assert "'/changes'" in js
        assert "'/discard'" in js
        assert "setInterval(() => {" in js
        assert "window.showToast" in js
        # Deeplink: the session-viewer's workspace-changes anchor lands
        # here with ?session=<tmux_name> and expects the review screen
        # to open directly. Without _handleDeeplink the page is just
        # the list view and the operator has to find their row.
        assert "_handleDeeplink()" in js
        assert "params.get('session')" in js

    def test_spa_router_knows_worktrees_route(self):
        app_js = (JS_DIR.parent / "app.js").read_text()
        assert "renderWorktreesFragment" in app_js
        assert "fetch('/pages/worktrees')" in app_js
        assert "path === '/worktrees'" in app_js
        assert "async function route()" in app_js
        assert "await _checkVersion();" in app_js
        assert "data-hard-reload" in app_js
        assert "_watchWorktreesBoot" not in app_js
        assert "window.location.reload();" not in app_js

    def test_template_uses_required_status_labels(self):
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()
        assert "LIVE" in js
        assert "ORPHANED" in js
        assert "DEAD-CLEAN" in js
        assert "Worktrees" in template
        assert "Commits" in template
        assert "Changes" in template
        assert "Sync Worktree to Latest" in template
        assert "Request Rebase" in template
        assert 'data-testid="rebase-required-dialog"' in template
        assert 'x-markdown="selectedCommit.commit.body"' in template
        assert 'x-text="fitPath(file.path, $el)"' in template
        assert 'x-text="repoName(item.row)"' in template
        assert 'x-text="row.session_title"' in template
        assert "changesCompanionCommitLabel(row)" in template
        assert "1 commit also present" in js
        assert 'x-text="uncommittedChangesCount"' in template
        assert 'data-testid="worktree-merge-disabled-reason"' in template
        assert 'x-show="canDiscardDirtyRow(row)"' in template
        assert 'x-text="refreshing ? \'Refreshing...\' : \'Refresh\'"' in template
        assert 'href="/worktrees"' in template
        assert 'data-hard-reload' in template
        assert '@click.prevent="refresh(true)"' in template
        assert template.lstrip().startswith('<div data-testid="worktrees-fragment-root">')
        assert '<style>' in template
        assert '<div x-data="worktreesPage()"' in template
        assert "worktreesBooted" not in js
        assert 'x-show="!refreshing"' not in template
        assert 'x-show="refreshing"' not in template
        assert "Are you sure you want to delete this Worktree?" in template
        # Session badge in the review screen links back to the session
        # viewer for live rows — both the commit-review and the
        # dirty-review badge route to /session/<project>/<tmux>.
        assert "selectedCommit.row.session_project" in template
        assert "selectedDirtyRow.session_project" in template
        assert "'/session/' + encodeURIComponent(selectedCommit.row.session_project)" in template
        assert "'/session/' + encodeURIComponent(selectedDirtyRow.session_project)" in template
        assert 'x-ref="dirtyDetailScroller"' in template
        assert 'x-ref="commitTitleSentinel"' in template
        assert 'x-ref="dirtyTitleSentinel"' in template
        assert 'x-ref="dirtyTitleBar"' in template
        assert 'x-ref="dirtyFilesHeader"' in template
        assert 'class="worktree-diff-code hljs whitespace-pre px-2 pr-3"' in template
        assert 'x-html="line.html || \'&nbsp;\'"' in template

    def test_style_first_fragments_have_wrapper_root(self):
        worktrees = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text().lstrip()
        collab = (TEMPLATE_DIR / "pages" / "collab.html").read_text().lstrip()
        design = (TEMPLATE_DIR / "pages" / "design.html").read_text().lstrip()

        assert worktrees.startswith('<div data-testid="worktrees-fragment-root">')
        assert collab.startswith('<div data-testid="collab-fragment-root">')
        assert design.startswith('<div data-testid="design-fragment-root">')

    def test_pr_badge_template_and_helpers_wired(self):
        """The PR badge fragment from the settled design (3435e03f) is in
        the template and the Alpine helpers ``rowPr`` / ``prBadgeClass`` /
        ``prDotClass`` / ``prIsFlashing`` are wired in worktrees.js."""
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()

        # Badge fragment renders only when rowPr returns a PR object.
        assert 'data-testid="pr-badge"' in template
        assert 'x-if="rowPr(item.row)"' in template
        assert ':class="prBadgeClass(rowPr(item.row))"' in template
        assert ':class="prDotClass(rowPr(item.row))"' in template
        assert "'PR #' + rowPr(item.row).number" in template

        # Helpers map source_control.review -> design's flatter pr shape.
        assert "rowPr(row) {" in js
        assert "row.source_control && row.source_control.review" in js
        assert "state: review.aggregate_state" in js  # green | yellow
        assert "running: review.running" in js
        assert "pr_checks: review.checks" in js
        # Visual classes come straight from the design — green is passing,
        # yellow is the not-passing color, animate-pulse is the running
        # overlay (only on green when watch is active).
        assert "border-emerald-300/20 bg-emerald-300/10 text-emerald-100" in js
        assert "border-amber-300/20 bg-amber-300/10 text-amber-100" in js
        assert "bg-emerald-300" in js
        assert "bg-amber-200" in js
        assert "animate-pulse" in js

    def test_pr_navigator_template_and_helpers_wired(self):
        """The on-card PR/commit navigator (settled design 3435e03f, lines
        205-258) renders only when the row has a PR, exposes one PR row
        plus one row per commit, and ties click handlers to
        ``openReviewPr`` / ``openReviewCommit``."""
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()

        # Navigator gate: only renders when rowPr is non-null.
        assert 'data-testid="pr-navigator"' in template
        assert 'data-testid="pr-navigator-pr-row"' in template
        assert 'data-testid="pr-navigator-commit-row"' in template
        # PR row binds to openReviewPr; per-commit rows bind to openReviewCommit.
        assert '@click="openReviewPr(item.row)"' in template
        assert '@click="openReviewCommit(item.row, idx)"' in template
        # Both rows render the icon disc strip with checkIconClass coloring.
        assert ':class="checkIconClass(check.status)"' in template
        assert 'x-text="check.icon"' in template
        # PR row uses rowPrChecks; commit rows use reviewCommitChecks.
        assert 'check in rowPrChecks(item.row)' in template
        assert 'check in reviewCommitChecks(commit)' in template

        # Helpers exist with the expected shapes.
        assert "rowPrChecks(row) {" in js
        assert "reviewCommitChecks(_commit)" in js  # Returns [] until per-commit data lands.
        assert "checkIconClass(status) {" in js
        assert "openReviewPr(row) {" in js
        assert "openReviewCommit(row, idx) {" in js
        assert "openReviewDefault(row) {" in js
        # Disc colors lifted from the design — emerald pass, amber running,
        # rose fail, white pending.
        assert "border-emerald-300/20 bg-emerald-300/12 text-emerald-100" in js
        assert "border-amber-300/20 bg-amber-300/12 text-amber-100" in js
        assert "border-rose-300/20 bg-rose-300/12 text-rose-100" in js


# ── Worktrees row-scoped GitHub operation surface (auto-ltibi) ─────────


class _DockerExecRecorder:
    """Capture ``run_cli`` calls made by the GitHub capability service and replay scripted
    ``(stdout, stderr, returncode, timed_out)`` tuples in order.

    First inspect call is treated as the container liveness probe and is
    stubbed independently from gh invocations.
    """

    def __init__(self, *, container_running=True, gh_results=None):
        self.calls: list[list[str]] = []
        self._container_running = container_running
        self._gh_results = list(gh_results or [])

    async def run_cli(self, cmd, *, timeout=30):
        self.calls.append(list(cmd))
        # docker inspect probe → liveness
        if cmd[:3] == ["docker", "inspect", "-f"]:
            running = "true" if self._container_running else "false"
            rc = 0 if self._container_running else 1
            return running, "", rc, False
        # docker exec ... gh ...  → scripted gh result
        if cmd[:2] == ["docker", "exec"]:
            if not self._gh_results:
                raise AssertionError(
                    f"unexpected docker exec call with no scripted result: {cmd!r}"
                )
            return self._gh_results.pop(0)
        raise AssertionError(f"unexpected run_cli command: {cmd!r}")


def _live_row(session="auto-test", repo="autonomy"):
    return _row(session=session, repo=repo, live=True)


def _install_github_stubs(
    monkeypatch,
    *,
    rows,
    container_running=True,
    gh_results=None,
    repo_slug="anchore/autonomy",
):
    """Stub the capability service for tests and return ``(wg, recorder, rows)``.

    The capability service no longer reaches into Dashboard caches —
    callers pass ``rows`` to each public op. Tests use the returned
    ``rows`` value to invoke ops with the same row list the recorder
    is rigged for.
    """
    from agents.capabilities.github import service as wg

    rows_list = list(rows)
    recorder = _DockerExecRecorder(
        container_running=container_running,
        gh_results=gh_results,
    )
    monkeypatch.setattr(wg, "run_cli", recorder.run_cli)
    monkeypatch.setattr(wg, "derive_repo_slug", lambda _path: repo_slug)
    return wg, recorder, rows_list


class TestWorktreeGithubResolution:
    def test_find_live_worktree_row_returns_only_live_match(self):
        from agents.capabilities.github import service as wg

        rows = [
            _row(session="auto-dead", repo="autonomy", live=False),
            _row(session="auto-live", repo="autonomy", live=True),
            _row(session="auto-live", repo="enterprise", live=True),
        ]

        live = wg.find_live_worktree_row("auto-live", "enterprise", rows)
        assert live is not None
        assert live.session_name == "auto-live"
        assert live.repo_name == "enterprise"

        dead = wg.find_live_worktree_row("auto-dead", "autonomy", rows)
        assert dead is None

    def test_find_live_worktree_row_returns_none_for_empty_rows(self):
        from agents.capabilities.github import service as wg

        match = wg.find_live_worktree_row("auto-x", "autonomy", [])
        assert match is None

    def test_classify_failure_maps_canonical_states(self):
        from agents.capabilities.github import service as wg

        assert wg.classify_failure("", "", 0, False) is None
        assert wg.classify_failure("", "timeout", -1, True) == wg.FAILURE_TIMED_OUT
        assert (
            wg.classify_failure("", 'OCI runtime exec failed: exec failed: unable to start container process: exec: "gh"', 126, False)
            == wg.FAILURE_GH_MISSING
        )
        assert wg.classify_failure("", "", 127, False) == wg.FAILURE_GH_MISSING
        assert (
            wg.classify_failure("", "Try authenticating with: gh auth login", 4, False)
            == wg.FAILURE_AUTH_MISSING
        )
        assert (
            wg.classify_failure("", "HTTP 401 Unauthorized", 1, False)
            == wg.FAILURE_AUTH_MISSING
        )
        assert wg.classify_failure("", "boom", 2, False) == wg.FAILURE_EXEC_FAILED


class TestWorktreePRSnapshot:
    def test_snapshot_runs_gh_pr_view_in_live_container(self, monkeypatch):
        wg, recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            gh_results=[(
                '{"number": 42, "state": "OPEN", "title": "Demo"}',
                "",
                0,
                False,
            )],
        )

        result = asyncio.run(
            wg.source_control_review_read_v1("auto-test", "autonomy", rows=rows)
        )

        assert result.ok is True
        assert result.failure is None
        assert result.operation == wg.OP_REVIEW_READ
        assert result.session_name == "auto-test"
        assert result.repo_name == "autonomy"
        assert result.exit_code == 0
        assert result.timed_out is False
        assert result.container_name == "auto-test"
        assert result.branch == "session/auto-test"
        assert result.repo_slug == "anchore/autonomy"
        assert result.stdout == '{"number": 42, "state": "OPEN", "title": "Demo"}'
        assert result.error_message is None

        # docker inspect ran first (liveness), then docker exec ... gh ...
        assert recorder.calls[0][:3] == ["docker", "inspect", "-f"]
        assert recorder.calls[0][-1] == "auto-test"
        gh_call = recorder.calls[1]
        assert gh_call[:4] == ["docker", "exec", "auto-test", "gh"]
        assert "pr" in gh_call and "view" in gh_call
        assert "session/auto-test" in gh_call
        assert "--repo" in gh_call
        assert "anchore/autonomy" in gh_call
        assert "--json" in gh_call
        # Same docker exec command surfaces in the result for diagnostics.
        assert result.command == gh_call

    def test_snapshot_returns_no_live_row_when_session_is_dead(self, monkeypatch):
        wg, recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_row(session="auto-dead", live=False)],
        )

        result = asyncio.run(
            wg.source_control_review_read_v1("auto-dead", "autonomy", rows=rows)
        )

        assert result.ok is False
        assert result.failure == wg.FAILURE_NO_LIVE_ROW
        assert result.container_name is None
        assert result.command == []
        assert "no live worktree row" in (result.error_message or "")
        # Must not exec into any container if the row isn't live.
        assert recorder.calls == []

    def test_snapshot_returns_no_live_container_when_inspect_fails(self, monkeypatch):
        wg, recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            container_running=False,
            gh_results=[],
        )

        result = asyncio.run(
            wg.source_control_review_read_v1("auto-test", "autonomy", rows=rows)
        )

        assert result.ok is False
        assert result.failure == wg.FAILURE_NO_LIVE_CONTAINER
        assert result.container_name is None
        # docker inspect was probed; docker exec was not.
        assert any(c[:3] == ["docker", "inspect", "-f"] for c in recorder.calls)
        assert not any(c[:2] == ["docker", "exec"] for c in recorder.calls)

    def test_snapshot_surfaces_gh_missing_when_exit_127(self, monkeypatch):
        wg, _recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            gh_results=[("", "gh: command not found", 127, False)],
        )

        result = asyncio.run(
            wg.source_control_review_read_v1("auto-test", "autonomy", rows=rows)
        )

        assert result.ok is False
        assert result.failure == wg.FAILURE_GH_MISSING
        assert result.exit_code == 127
        assert "command not found" in (result.error_message or "")

    def test_snapshot_surfaces_auth_missing_when_gh_says_login(self, monkeypatch):
        wg, _recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            gh_results=[("", "Run `gh auth login` to authenticate.", 4, False)],
        )

        result = asyncio.run(
            wg.source_control_review_read_v1("auto-test", "autonomy", rows=rows)
        )

        assert result.ok is False
        assert result.failure == wg.FAILURE_AUTH_MISSING
        assert "gh auth login" in (result.error_message or "")

    def test_snapshot_surfaces_timeout(self, monkeypatch):
        wg, _recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            gh_results=[("", "timeout after 30s", -1, True)],
        )

        result = asyncio.run(
            wg.source_control_review_read_v1("auto-test", "autonomy", rows=rows)
        )

        assert result.ok is False
        assert result.failure == wg.FAILURE_TIMED_OUT
        assert result.timed_out is True

    def test_snapshot_surfaces_no_repo_slug_when_remote_unparseable(self, monkeypatch):
        from agents.capabilities.github import service as wg

        rows = [_live_row()]
        monkeypatch.setattr(wg, "derive_repo_slug", lambda _p: None)
        # run_cli must NOT be called — we should fail before reaching docker.
        async def _explode(*a, **k):
            raise AssertionError("run_cli should not be invoked when repo slug is missing")
        monkeypatch.setattr(wg, "run_cli", _explode)

        result = asyncio.run(
            wg.source_control_review_read_v1("auto-test", "autonomy", rows=rows)
        )

        assert result.ok is False
        assert result.failure == wg.FAILURE_NO_REPO_SLUG
        assert result.container_name is None


class TestWorktreePRRefresh:
    def test_refresh_runs_same_gh_pr_view_template_as_snapshot(self, monkeypatch):
        wg, recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            gh_results=[("{}", "", 0, False)],
        )

        result = asyncio.run(
            wg.source_control_review_refresh_v1("auto-test", "autonomy", rows=rows)
        )

        assert result.ok is True
        assert result.operation == wg.OP_REVIEW_REFRESH
        gh_call = recorder.calls[1]
        assert gh_call[:4] == ["docker", "exec", "auto-test", "gh"]
        assert "pr" in gh_call and "view" in gh_call
        assert "session/auto-test" in gh_call


class TestWorktreePRWatchSet:
    @pytest.mark.parametrize(
        ("mode", "expected_subscribed", "expected_ignored", "expected_method"),
        [
            ("subscribed", "subscribed=true", "ignored=false", "PUT"),
            ("ignored", "subscribed=false", "ignored=true", "PUT"),
        ],
    )
    def test_watch_set_put_subscribed_and_ignored(
        self, monkeypatch, mode, expected_subscribed, expected_ignored, expected_method,
    ):
        wg, recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            gh_results=[("", "", 0, False)],
        )

        result = asyncio.run(
            wg.source_control_gates_watch_set_v1("auto-test", "autonomy", mode, rows=rows)
        )

        assert result.ok is True
        assert result.operation == wg.OP_GATES_WATCH_SET
        gh_call = recorder.calls[1]
        assert gh_call[:4] == ["docker", "exec", "auto-test", "gh"]
        assert "api" in gh_call
        assert expected_method in gh_call
        assert "/repos/anchore/autonomy/subscription" in gh_call
        assert expected_subscribed in gh_call
        assert expected_ignored in gh_call

    def test_watch_set_default_clears_subscription_via_delete(self, monkeypatch):
        wg, recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            gh_results=[("", "", 0, False)],
        )

        result = asyncio.run(
            wg.source_control_gates_watch_set_v1("auto-test", "autonomy", "default", rows=rows)
        )

        assert result.ok is True
        gh_call = recorder.calls[1]
        assert "DELETE" in gh_call
        assert "/repos/anchore/autonomy/subscription" in gh_call

    def test_watch_set_rejects_unknown_mode_without_executing(self, monkeypatch):
        wg, recorder, rows = _install_github_stubs(
            monkeypatch,
            rows=[_live_row()],
            gh_results=[],
        )

        result = asyncio.run(
            wg.source_control_gates_watch_set_v1("auto-test", "autonomy", "muted", rows=rows)
        )

        assert result.ok is False
        assert result.failure == wg.FAILURE_INVALID_MODE
        assert "muted" in (result.error_message or "")
        # Invalid mode short-circuits before ANY docker call — including
        # the inspect liveness probe — to avoid touching infra on bad input.
        assert recorder.calls == []


class TestWorktreeGithubExecResultSerialization:
    def test_to_dict_round_trips_all_fields(self):
        from agents.capabilities.github import service as wg

        result = wg.WorktreeGithubExecResult(
            operation=wg.OP_REVIEW_READ,
            session_name="auto-x",
            repo_name="autonomy",
            ok=False,
            stdout="payload",
            stderr="err",
            exit_code=127,
            timed_out=False,
            container_name="auto-x",
            branch="session/auto-x",
            repo_slug="anchore/autonomy",
            command=["docker", "exec", "auto-x", "gh", "pr", "view"],
            failure=wg.FAILURE_GH_MISSING,
            error_message="gh CLI is not installed in the live container",
        )
        data = result.to_dict()
        assert data["operation"] == wg.OP_REVIEW_READ
        assert data["ok"] is False
        assert data["failure"] == wg.FAILURE_GH_MISSING
        assert data["command"] == ["docker", "exec", "auto-x", "gh", "pr", "view"]
        assert data["exit_code"] == 127
        assert data["timed_out"] is False
        assert data["container_name"] == "auto-x"
        assert data["branch"] == "session/auto-x"
        assert data["repo_slug"] == "anchore/autonomy"
        assert data["error_message"] == "gh CLI is not installed in the live container"


# ── Review payload normalization ──────────────────────────────────────


class TestNormalizeReviewPayload:
    def test_returns_none_for_empty_or_whitespace(self):
        from agents.capabilities.github import service as wg

        assert wg.normalize_review_payload("") is None
        assert wg.normalize_review_payload("   \n  ") is None

    def test_returns_none_for_unparseable_or_non_object(self):
        from agents.capabilities.github import service as wg

        assert wg.normalize_review_payload("not json") is None
        assert wg.normalize_review_payload("[1, 2, 3]") is None

    def test_minimal_payload_yields_green_aggregate_with_no_checks(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 42, "state": "OPEN", "title": "Add thing", "body": "body",'
            ' "url": "https://github.com/x/y/pull/42", "headRefName": "feat",'
            ' "headRefOid": "abc1234", "baseRefName": "main", "isDraft": false,'
            ' "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",'
            ' "statusCheckRollup": []}'
        )
        review = wg.normalize_review_payload(raw)
        assert review is not None
        assert review["number"] == 42
        assert review["title"] == "Add thing"
        assert review["body"] == "body"
        assert review["url"] == "https://github.com/x/y/pull/42"
        assert review["head_sha"] == "abc1234"
        assert review["base_branch"] == "main"
        assert review["state"] == "open"
        assert review["is_draft"] is False
        assert review["aggregate_state"] == "green"
        assert review["running"] is False
        assert review["checks"] == []

    def test_check_run_completed_success_normalizes_to_pass(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review["aggregate_state"] == "green"
        assert review["running"] is False
        assert review["checks"] == [
            {"id": "build", "icon": "B", "label": "build", "status": "pass", "detail": None}
        ]

    def test_check_run_completed_failure_marks_yellow(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review["aggregate_state"] == "yellow"
        assert review["running"] is False
        assert review["checks"][0]["status"] == "fail"

    def test_in_progress_check_marks_running_overlay_not_color(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "name": "tests", "status": "IN_PROGRESS"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        # Running is a separate overlay — color stays green when nothing has failed yet.
        assert review["aggregate_state"] == "green"
        assert review["running"] is True
        assert review["checks"][0]["status"] == "running"

    def test_status_context_state_failure_marks_yellow(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "StatusContext", "context": "ci/circleci", "state": "FAILURE",'
            ' "description": "step failed: build"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review["aggregate_state"] == "yellow"
        # ``ci/circleci`` strips the ``ci/`` prefix and yields ``C`` (a single
        # alpha glyph from the trailing token); navigator collisions across
        # CI providers are disambiguated by the full ``label``.
        assert review["checks"] == [{
            "id": "ci/circleci",
            "icon": "C",
            "label": "ci/circleci",
            "status": "fail",
            "detail": "step failed: build",
        }]

    def test_skipped_check_is_filtered_out(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "name": "optional", "status": "COMPLETED", "conclusion": "SKIPPED"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review["checks"] == []

    def test_changes_requested_review_marks_yellow(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": [],'
            ' "reviewDecision": "CHANGES_REQUESTED"}'
        )
        review = wg.normalize_review_payload(raw)
        assert review["aggregate_state"] == "yellow"

    def test_conflicting_mergeable_marks_yellow(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": [],'
            ' "mergeable": "CONFLICTING"}'
        )
        review = wg.normalize_review_payload(raw)
        assert review["aggregate_state"] == "yellow"

    def test_unrecognized_rollup_entry_is_dropped_not_raised(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"weird": "shape"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review["checks"] == []
        assert review["aggregate_state"] == "green"

    def test_check_icon_derives_glyph_from_label(self):
        """``icon`` is a 1–2 char glyph derived from the check label so
        the navigator can render disc-sized badges. Path prefixes are
        stripped; multi-token labels yield two-char glyphs."""
        from agents.capabilities.github import service as wg

        # Single-token label → first letter uppercased.
        assert wg._icon_from_label("build") == "B"
        assert wg._icon_from_label("test") == "T"
        # Path prefix is stripped (last segment after / wins).
        assert wg._icon_from_label("ci/circleci") == "C"
        # Multi-token labels (separated by space, dash, underscore) yield
        # the first alpha char of each of the first two tokens.
        assert wg._icon_from_label("build_lint") == "BL"
        assert wg._icon_from_label("integration test") == "IT"
        assert wg._icon_from_label("e2e-suite") == "ES"
        # Defensive: empty / non-alpha → ``?``.
        assert wg._icon_from_label("") == "?"
        assert wg._icon_from_label("   ") == "?"
        assert wg._icon_from_label("///") == "?"

    def test_running_check_carries_icon(self):
        """End-to-end: an in-progress check normalizes with both status
        and icon so the UI's check disc renders correctly."""
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "name": "tests", "status": "IN_PROGRESS"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review["checks"] == [{
            "id": "tests",
            "icon": "T",
            "label": "tests",
            "status": "running",
            "detail": None,
        }]


# ── Capability probe (autonomy/github) ────────────────────────────────


class TestGithubProbe:
    def _patched_probe(self, monkeypatch, *, container_running, gh_results):
        from agents.capabilities.github import probe as gh_probe
        from agents.capabilities.github import service as wg

        recorder = _DockerExecRecorder(
            container_running=container_running,
            gh_results=gh_results,
        )
        # Probe imports run_cli + resolve_live_container from service; patch service.
        monkeypatch.setattr(wg, "run_cli", recorder.run_cli)
        return gh_probe, recorder

    def test_no_live_container_returns_unavailable(self, monkeypatch):
        gh_probe, _recorder = self._patched_probe(
            monkeypatch, container_running=False, gh_results=[],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-dead"))

        assert result["state"] == "unavailable"
        assert result["reason"] == "no_live_container"
        assert result["contract"] == "source_control"
        assert result["implementation"] == "autonomy/github"
        assert result["delivery_mode"] == "image_baked"
        assert result["missing_tools"] == []
        assert result["missing_env"] == []

    def test_ready_when_gh_auth_status_succeeds(self, monkeypatch):
        gh_probe, recorder = self._patched_probe(
            monkeypatch,
            container_running=True,
            gh_results=[("Logged in to github.com as foo", "", 0, False)],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-live"))

        assert result["state"] == "ready"
        assert result["reason"] is None
        assert result["missing_tools"] == []
        assert result["missing_env"] == []
        # docker exec hit gh auth status, not gh pr view.
        assert recorder.calls[-1][3:] == ["gh", "auth", "status"]

    def test_gh_missing_marks_degraded_with_missing_tool(self, monkeypatch):
        gh_probe, _recorder = self._patched_probe(
            monkeypatch,
            container_running=True,
            gh_results=[("", "executable file not found in $PATH", 127, False)],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-live"))

        assert result["state"] == "degraded"
        assert result["reason"] == "tool_missing"
        assert result["missing_tools"] == ["gh"]
        assert result["missing_env"] == []

    def test_auth_missing_marks_degraded_with_missing_env(self, monkeypatch):
        gh_probe, _recorder = self._patched_probe(
            monkeypatch,
            container_running=True,
            gh_results=[("", "You are not logged into any GitHub hosts. Run gh auth login", 1, False)],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-live"))

        assert result["state"] == "degraded"
        assert result["reason"] == "env_missing"
        assert result["missing_env"] == ["GH_TOKEN"]
        assert result["missing_tools"] == []

    def test_other_failure_marks_degraded_probe_failed_with_details(self, monkeypatch):
        gh_probe, _recorder = self._patched_probe(
            monkeypatch,
            container_running=True,
            gh_results=[("", "weird state", 7, False)],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-live"))

        assert result["state"] == "degraded"
        assert result["reason"] == "probe_failed"
        assert result["details"]["exit_code"] == 7
        assert "weird state" in result["details"]["stderr"]


# ── WorktreeMonitor source_control composition ────────────────────────


class TestWorktreeMonitorCapabilityCache:
    """Refresh fans out source_control fetches for live rows; the result
    is cached and exposed via ``get_source_control``.
    """

    def _make_monitor(self, monkeypatch, *, rows, snapshots=None, exceptions=None):
        from tools.dashboard import worktree_monitor as wm_module

        snapshots = snapshots or {}
        exceptions = exceptions or {}

        async def fake_fetch(row, all_rows):
            key = (row.session_name, row.repo_name)
            if key in exceptions:
                raise exceptions[key]
            return snapshots.get(key, {
                "state": "ready",
                "implementation": "autonomy/github",
                "reason": None,
                "review": None,
            })

        async def fake_scan_thread():  # to_thread expects sync; sub via attr
            return list(rows)

        monkeypatch.setattr(wm_module, "_fetch_source_control", fake_fetch)
        monkeypatch.setattr(wm_module, "scan_all_worktrees", lambda: list(rows))
        return wm_module.WorktreeMonitor()

    def test_refresh_caches_snapshots_for_live_rows_only(self, monkeypatch):
        rows = [
            _row(session="auto-live", live=True),
            _row(session="auto-dead", live=False),
        ]
        snapshot = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": {"number": 7},
        }
        monitor = self._make_monitor(
            monkeypatch,
            rows=rows,
            snapshots={("auto-live", "autonomy"): snapshot},
        )

        asyncio.run(monitor.refresh())

        assert monitor.get_source_control("auto-live", "autonomy") == snapshot
        # Non-live row got no fetch and therefore no cache entry.
        assert monitor.get_source_control("auto-dead", "autonomy") is None

    def test_refresh_drops_cache_when_no_live_rows(self, monkeypatch):
        # Seed the cache via one refresh, then refresh again with no live rows.
        rows_first = [_row(session="auto-live", live=True)]
        snapshot = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": None,
        }
        monitor = self._make_monitor(
            monkeypatch,
            rows=rows_first,
            snapshots={("auto-live", "autonomy"): snapshot},
        )
        asyncio.run(monitor.refresh())
        assert monitor.get_source_control("auto-live", "autonomy") is not None

        from tools.dashboard import worktree_monitor as wm_module
        monkeypatch.setattr(wm_module, "scan_all_worktrees", lambda: [])
        asyncio.run(monitor.refresh())

        assert monitor.get_source_control("auto-live", "autonomy") is None

    def test_refresh_swallows_per_row_exception_with_degraded_marker(self, monkeypatch):
        rows = [_row(session="auto-live", live=True)]
        monitor = self._make_monitor(
            monkeypatch,
            rows=rows,
            exceptions={("auto-live", "autonomy"): RuntimeError("boom")},
        )

        # Refresh must not raise even though the per-row fetch did.
        asyncio.run(monitor.refresh())

        snapshot = monitor.get_source_control("auto-live", "autonomy")
        assert snapshot is not None
        assert snapshot["state"] == "degraded"
        assert snapshot["reason"] == "probe_failed"
        assert snapshot["review"] is None


# ── /api/worktrees row JSON includes source_control when cached ───────


class TestWorktreeApiSourceControlBlock:
    """Row JSON emits ``source_control`` only when the monitor has a
    cached snapshot. Non-live rows must not carry the field.
    """

    def test_get_worktrees_includes_source_control_when_cached(
        self, test_client, monkeypatch,
    ):
        from tools.dashboard import server

        rows = [_row(session="auto-live", live=True)]
        snapshot = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": {
                "number": 99,
                "url": "https://github.com/x/y/pull/99",
                "title": "Wire it",
                "body": "",
                "head_sha": "deadbeef",
                "base_branch": "main",
                "state": "open",
                "is_draft": False,
                "aggregate_state": "green",
                "running": False,
                "checks": [],
            },
        }

        monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: list(rows))
        monkeypatch.setattr(
            server.worktree_monitor,
            "get_source_control",
            lambda session, repo: snapshot if (session, repo) == ("auto-live", "autonomy") else None,
        )

        resp = test_client.get("/api/worktrees")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["source_control"] == snapshot

    def test_get_worktrees_omits_source_control_when_not_cached(
        self, test_client, monkeypatch,
    ):
        from tools.dashboard import server

        rows = [_row(session="auto-dead", live=False)]
        monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: list(rows))
        monkeypatch.setattr(
            server.worktree_monitor, "get_source_control", lambda *_: None,
        )

        resp = test_client.get("/api/worktrees")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert "source_control" not in data[0]
