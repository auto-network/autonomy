"""HTTP and wiring tests for the Worktrees dashboard page."""

import asyncio
import time
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
        self.last_force = None

    def get_all(self):
        return list(self.rows)

    async def refresh(self, *, force_capabilities: bool = False):
        self.refresh_count += 1
        self.last_force = force_capabilities
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
        # Top-level refresh is local-git only — must NOT pass force_capabilities=True.
        # The per-row endpoint below is the operator's force-GET path.
        assert fake.last_force is False

    def test_per_row_refresh_endpoint_force_fetches_one_row(
        self, test_client, monkeypatch,
    ):
        """``POST /api/worktrees/{session}/{repo}/refresh`` is the
        operator-explicit force-GET path: scoped to one row, bypasses
        TTL + poll budget, but stays per-row so a click on Refresh
        inside the overlay doesn't fan out gh calls for every live
        row."""
        from tools.dashboard import server

        rows = [_row(session="auto-x", live=True)]
        captured = {"calls": []}

        async def fake_refresh_one(session, repo):
            captured["calls"].append((session, repo))
            return list(rows)

        monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: list(rows))
        monkeypatch.setattr(
            server.worktree_monitor, "refresh_one", fake_refresh_one,
        )

        resp = test_client.post("/api/worktrees/auto-x/autonomy/refresh")

        assert resp.status_code == 200
        body = resp.json()
        assert body["session_name"] == "auto-x"
        assert body["repo_name"] == "autonomy"
        # refresh_one was called with the right (session, repo).
        assert captured["calls"] == [("auto-x", "autonomy")]

    def test_per_row_refresh_returns_404_when_row_missing(
        self, test_client, monkeypatch,
    ):
        from tools.dashboard import server

        async def fake_refresh_one(session, repo):
            return []  # no matching row in the scan

        monkeypatch.setattr(
            server.worktree_monitor, "refresh_one", fake_refresh_one,
        )

        resp = test_client.post("/api/worktrees/missing/autonomy/refresh")

        assert resp.status_code == 404
        assert resp.json()["error"] == "worktree not found"

    def test_per_row_refresh_returns_200_for_dead_session(
        self, test_client, monkeypatch,
    ):
        """A dead-session row is still a real worktree — return 200 with
        the row JSON (session_live=False). The capability fetch was
        skipped (no container to docker exec into) but the local-git
        rescan and cached source_control are still served. Per
        graph://d9764756-c49: dead-session re-resolution belongs to a
        future ``host_proxy`` capability path, not to per-row refresh.
        """
        from tools.dashboard import server

        dead_row = _row(session="auto-dead", live=False)

        async def fake_refresh_one(session, repo):
            return [dead_row]

        monkeypatch.setattr(
            server.worktree_monitor, "refresh_one", fake_refresh_one,
        )

        resp = test_client.post("/api/worktrees/auto-dead/autonomy/refresh")

        assert resp.status_code == 200
        body = resp.json()
        assert body["session_name"] == "auto-dead"
        assert body["session_live"] is False

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

    def test_pr_diff_endpoint_returns_integrated_patch(self, test_client, monkeypatch):
        """``GET /api/worktrees/{session}/{repo}/pr-diff`` returns the
        ``merge-base..HEAD`` integrated diff in the same shape as
        ``/changes``. Powers the auto-r098a PR-mode review overlay."""
        server, _fake = _install_fake_monitor(monkeypatch, [_row(live=True)])

        def fake_detail(session_name, repo_name, *, base_sha=None, head_sha=None):
            assert (session_name, repo_name) == ("auto-test", "autonomy")
            return server.WorktreeDirtyDetail(
                files=[
                    GitFileChange(status="M", path="a.txt", additions=8, deletions=2),
                    GitFileChange(status="A", path="b.txt", additions=3, deletions=0),
                ],
                patch="diff --git a/a.txt b/a.txt\ndiff --git a/b.txt b/b.txt",
            )

        monkeypatch.setattr(server, "get_session_worktree_integrated_diff", fake_detail)

        resp = test_client.get("/api/worktrees/auto-test/autonomy/pr-diff")

        assert resp.status_code == 200
        body = resp.json()
        assert body["patch"].startswith("diff --git a/a.txt")
        assert body["files"] == [
            {"status": "M", "path": "a.txt", "additions": 8, "deletions": 2},
            {"status": "A", "path": "b.txt", "additions": 3, "deletions": 0},
        ]

    def test_pr_diff_endpoint_surfaces_workspace_error_as_404(self, test_client, monkeypatch):
        from tools.dashboard import server

        def fake_detail(*_args, **_kwargs):
            raise WorkspaceError("could not resolve base ref")

        monkeypatch.setattr(server, "get_session_worktree_integrated_diff", fake_detail)

        resp = test_client.get("/api/worktrees/missing/autonomy/pr-diff")

        assert resp.status_code == 404
        assert "could not resolve base ref" in resp.json()["error"]

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
        the template and the Alpine helpers ``rowPrs`` / ``prBadgeClass`` /
        ``prDotClass`` / ``prIsFlashing`` are wired in worktrees.js.

        Post-binding migration (auto-nrqbs): badges loop over
        ``rowPrs(item.row)`` to support stacked PRs; the singular
        ``rowPr`` survives as a back-compat alias for first-or-null.
        """
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()

        # Badge fragment renders one badge per PR (zero, one, or many).
        assert 'data-testid="pr-badge"' in template
        assert 'x-for="pr in rowPrs(item.row)"' in template
        assert ':class="prBadgeClass(pr)"' in template
        assert ':class="prDotClass(pr)"' in template

        # Helpers expose both plural ``rowPrs`` and singular ``rowPr``.
        assert "rowPrs(row) {" in js
        assert "rowPr(row) {" in js
        assert "row.source_control" in js
        # Adapter: cache → flat pr shape, with derived state/checks.
        assert "_adaptReview(review, mode) {" in js
        assert "running: !!review.running" in js
        assert "pr_checks: checks" in js
        # Visual classes come straight from the design — green is passing,
        # yellow is the not-passing color, animate-pulse is the running
        # overlay (only on green when watch is active).
        assert "border-emerald-300/20 bg-emerald-300/10 text-emerald-100" in js
        assert "border-amber-300/20 bg-amber-300/10 text-amber-100" in js
        assert "bg-emerald-300" in js
        assert "bg-amber-200" in js
        assert "animate-pulse" in js

    def test_check_tooltip_popover_wired(self):
        """The rich check-tooltip popover (settled design 3435e03f, lines
        779-788) replaces the native ``title`` attribute on each
        navigator disc. Hover/focus opens it; click-outside / Escape
        closes it; click-on-disc toggles."""
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()

        # Tooltip element with the right anchor + transition.
        assert 'data-testid="check-tooltip"' in template
        assert 'x-show="checkTooltip.visible"' in template
        assert "x-transition.opacity.duration.75ms" in template
        assert "'left:' + checkTooltip.x + 'px; top:' + checkTooltip.y + 'px;'" in template
        # Outside-click + Escape close.
        assert '@click.window="hideCheckTooltip()"' in template
        assert '@keydown.escape.window="hideCheckTooltip()"' in template
        # Discs bind hover/focus/click — no native ``title`` attribute.
        assert '@mouseenter="showCheckTooltip($event, check)"' in template
        assert '@mouseleave="hideCheckTooltip()"' in template
        assert '@focus="showCheckTooltip($event, check)"' in template
        assert '@click.stop.prevent="toggleCheckTooltip($event, check)"' in template
        # The old native title attribute must be gone (we replaced it
        # specifically because it didn't carry the rich detail body).
        assert ":title=\"check.label" not in template

        # JS state + helpers exist.
        assert "checkTooltip:" in js
        assert "showCheckTooltip(event, check) {" in js
        assert "hideCheckTooltip()" in js
        assert "toggleCheckTooltip(event, check) {" in js
        assert "tooltipAnchorPoint(event)" in js
        assert "describeCheckStatus(status) {" in js
        # Tooltip text format matches the design ("label — passing/running/...").
        assert "describeCheckStatus(check.status)" in js

    def test_pr_review_overlay_wired(self):
        """The overlay (settled design 3435e03f, lines 460+) renders the
        PR title/body + integrated diff when ``selectedCommit.prMode``
        is true. PR-row click on the navigator opens this view; pager
        and per-commit merge buttons are hidden in PR mode."""
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()

        # Template fork on isPrReview().
        assert "isPrReview()" in template
        assert 'data-testid="review-pr-badge"' in template
        # Pager + commit-merge buttons gated off in PR mode.
        assert '!isPrReview() && selectedCommit.total > 1' in template
        assert '!isPrReview() && supportsDashboardMerge' in template
        # PR badge in the overlay header reuses prBadgeClass / prDotClass.
        assert ':class="prBadgeClass(selectedCommit.pr)"' in template
        assert ':class="prDotClass(selectedCommit.pr)"' in template
        assert "'PR #' + selectedCommit.pr.number" in template

        # JS: openReviewPr now actually fetches /pr-diff and slots the
        # integrated diff into selectedCommit with prMode: true. Stack
        # support (auto-nrqbs) added an explicit ``pr`` arg so per-PR
        # navigator clicks can scope ``?review_id=`` to the right row.
        assert "openReviewPr(row, pr)" in js
        assert "'/pr-diff'" in js
        assert "prMode: true" in js
        assert "isPrReview()" in js
        # PR-mode commit subject comes from the PR title; body from PR body.
        assert "subject: pr.title" in js
        assert "body: pr.body" in js

    def test_per_row_refresh_button_wired(self):
        """The review overlay's Refresh button posts to the per-row
        force-GET endpoint — restoring the operator's force-fetch
        affordance after the top-level refresh became local-git only.
        """
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()

        # Button rendered + disabled-while-loading wired.
        assert 'data-testid="review-overlay-refresh-button"' in template
        assert 'rowRefreshing' in template
        assert "@click=\"refreshSelectedRow()\"" in template

        # JS handler exists and POSTs to the per-row endpoint.
        assert "refreshSelectedRow()" in js
        assert "rowRefreshing" in js
        assert "'/refresh'" in js
        assert "method: 'POST'" in js
        # Updates this.rows in place so the cards page also sees fresh
        # state without waiting for the next /api/worktrees poll.
        assert "this.rows.splice(idx, 1, updated)" in js
        assert "this.syncOverlayRows()" in js

    def test_pr_nag_controls_wired(self):
        """The card-level Silent / Nag All Changes / Nag When Done
        controls (settled design 3435e03f, lines 485-498) bind to the
        rowNagMode / setNagMode helpers and route writes through
        PUT /api/worktrees/{session}/{repo}/watch."""
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()

        # All three buttons render with the design's labels.
        assert 'data-testid="pr-nag-controls"' in template
        assert 'data-testid="pr-nag-silent"' in template
        assert 'data-testid="pr-nag-all"' in template
        assert 'data-testid="pr-nag-done"' in template
        assert ">Silent<" in template
        assert ">Nag All Changes<" in template
        assert ">Nag When Done<" in template
        # Buttons drive setNagMode with the backend's mode strings.
        assert "setNagMode(item.row, 'silent')" in template
        assert "setNagMode(item.row, 'nag_all')" in template
        assert "setNagMode(item.row, 'nag_done')" in template

        # JS helpers exist and read source_control.watch.mode.
        assert "rowNagMode(row) {" in js
        assert "row.source_control && row.source_control.watch" in js
        assert "nagButtonClass(row, mode) {" in js
        assert "setNagMode(row, mode) {" in js
        # Watch writes go through the new endpoint.
        assert "'/watch'" in js
        assert "method: 'PUT'" in js
        # rowPr derives watch_active from nag mode (not literal false).
        assert "mode !== 'silent'" in js

    def test_pr_navigator_template_and_helpers_wired(self):
        """The on-card PR/commit navigator (settled design 3435e03f, lines
        205-258) renders only when the row has a PR, exposes one PR row
        plus one row per commit, and ties click handlers to
        ``openReviewPr`` / ``openReviewCommit``."""
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()

        # Navigator gate: renders when at least one PR is bound to the row.
        assert 'data-testid="pr-navigator"' in template
        assert 'data-testid="pr-navigator-pr-row"' in template
        assert 'data-testid="pr-navigator-commit-row"' in template
        # PR rows loop over rowPrs (one entry per binding for stacked PRs);
        # per-commit rows bind to openReviewCommit unchanged.
        assert 'x-if="rowPrs(item.row).length"' in template
        assert 'x-for="pr in rowPrs(item.row)"' in template
        assert '@click="openReviewPr(item.row, pr)"' in template
        assert '@click="openReviewCommit(item.row, idx)"' in template
        # Both rows render the icon disc strip with checkIconClass coloring.
        assert ':class="checkIconClass(check.status)"' in template
        assert 'x-text="check.icon"' in template
        # PR row uses rowPrChecks(item.row, pr) (per-PR checks); commit
        # rows use reviewCommitChecks.
        assert 'check in rowPrChecks(item.row, pr)' in template
        assert 'check in reviewCommitChecks(commit)' in template

        # Helpers exist with the expected shapes.
        assert "rowPrChecks(row, pr)" in js
        assert "reviewCommitChecks(_commit)" in js  # Returns [] until per-commit data lands.
        assert "checkIconClass(status) {" in js
        assert "openReviewPr(row, pr) {" in js
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
        # ``gh pr list --head <branch>`` surfaces every PR on the ref so
        # stacked PRs (multiple PRs sharing one branch) all show up.
        assert "pr" in gh_call and "list" in gh_call
        assert "--head" in gh_call
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
        assert "pr" in gh_call and "list" in gh_call
        assert "--head" in gh_call
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
        assert review.number == 42
        assert review.title == "Add thing"
        assert review.body == "body"
        assert review.url == "https://github.com/x/y/pull/42"
        assert review.head_sha == "abc1234"
        assert review.base_branch == "main"
        assert review.state == "open"
        assert review.is_draft is False
        assert review.aggregate_state == "green"
        assert review.running is False
        assert review.checks == ()

    def test_check_run_completed_success_normalizes_to_pass(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review.aggregate_state == "green"
        assert review.running is False
        assert [c.to_dict() for c in review.checks] == [
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
        assert review.aggregate_state == "yellow"
        assert review.running is False
        assert review.checks[0].status == "fail"

    def test_in_progress_check_marks_running_overlay_not_color(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "name": "tests", "status": "IN_PROGRESS"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        # Running is a separate overlay — color stays green when nothing has failed yet.
        assert review.aggregate_state == "green"
        assert review.running is True
        assert review.checks[0].status == "running"

    def test_status_context_state_failure_marks_yellow(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"__typename": "StatusContext", "context": "ci/circleci", "state": "FAILURE",'
            ' "description": "step failed: build"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review.aggregate_state == "yellow"
        # ``ci/circleci`` strips the ``ci/`` prefix and yields ``C`` (a single
        # alpha glyph from the trailing token); navigator collisions across
        # CI providers are disambiguated by the full ``label``.
        assert [c.to_dict() for c in review.checks] == [{
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
        assert review.checks == ()

    def test_changes_requested_review_marks_yellow(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": [],'
            ' "reviewDecision": "CHANGES_REQUESTED"}'
        )
        review = wg.normalize_review_payload(raw)
        assert review.aggregate_state == "yellow"

    def test_conflicting_mergeable_marks_yellow(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": [],'
            ' "mergeable": "CONFLICTING"}'
        )
        review = wg.normalize_review_payload(raw)
        assert review.aggregate_state == "yellow"

    def test_unrecognized_rollup_entry_is_dropped_not_raised(self):
        from agents.capabilities.github import service as wg

        raw = (
            '{"number": 1, "state": "OPEN", "statusCheckRollup": ['
            '{"weird": "shape"}'
            ']}'
        )
        review = wg.normalize_review_payload(raw)
        assert review.checks == ()
        assert review.aggregate_state == "green"

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

    def test_stack_orders_prs_by_local_commit_position(self):
        """``normalize_review_stack`` orders stacked PRs by which PR's
        last claimed commit comes later in local rev-list order, and
        chains each PR's ``base_sha`` to the previous PR's head_sha so
        per-PR diffs scope correctly."""
        from agents.capabilities.github import service as wg

        # gh returns the array in arbitrary order. Two PRs on one ref:
        # PR #303 claims commit ``ddd``, PR #302 claims commit ``ccc``.
        # Local rev-list order is ccc -> ddd -> eee, so #302 comes
        # before #303 in the stack.
        raw = (
            '[{"number": 303, "state": "OPEN", "title": "step 2",'
            ' "url": "https://x/y/pull/303", "headRefOid": "ddd",'
            ' "baseRefOid": "ccc", "baseRefName": "main",'
            ' "statusCheckRollup": [],'
            ' "commits": [{"oid": "ddd"}]},'
            '{"number": 302, "state": "OPEN", "title": "step 1",'
            ' "url": "https://x/y/pull/302", "headRefOid": "ccc",'
            ' "baseRefOid": "main_base", "baseRefName": "main",'
            ' "statusCheckRollup": [],'
            ' "commits": [{"oid": "ccc"}]}]'
        )
        stack = wg.normalize_review_stack(
            raw, local_commit_shas=("ccc", "ddd", "eee"),
        )
        assert len(stack) == 2
        # Ordered: #302 first (its commit ccc lands at index 0 of local),
        # then #303 (its commit ddd at index 1).
        assert [r.number for r in stack] == [302, 303]
        # First PR keeps its baseRefOid; second PR's base chains to first's head.
        assert stack[0].base_sha == "main_base"
        assert stack[1].base_sha == "ccc"
        # commit_shas mirror the local-order subset each PR claims.
        assert stack[0].commit_shas == ("ccc",)
        assert stack[1].commit_shas == ("ddd",)

    def test_stack_returns_empty_for_no_open_prs(self):
        """``gh pr list`` returns ``[]`` when the branch has no open PRs."""
        from agents.capabilities.github import service as wg

        assert wg.normalize_review_stack("[]") == ()

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
        assert [c.to_dict() for c in review.checks] == [{
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

        assert result.state == "unavailable"
        assert result.reason == "no_live_container"
        assert result.contract == "source_control"
        assert result.implementation == "autonomy/github"
        assert result.delivery_mode == "image_baked"
        assert result.missing_tools == ()
        assert result.missing_env == ()

    def test_ready_when_gh_auth_status_succeeds(self, monkeypatch):
        gh_probe, recorder = self._patched_probe(
            monkeypatch,
            container_running=True,
            gh_results=[("Logged in to github.com as foo", "", 0, False)],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-live"))

        assert result.state == "ready"
        assert result.reason is None
        assert result.missing_tools == ()
        assert result.missing_env == ()
        # docker exec hit gh auth status, not gh pr view.
        assert recorder.calls[-1][3:] == ["gh", "auth", "status"]

    def test_gh_missing_marks_degraded_with_missing_tool(self, monkeypatch):
        gh_probe, _recorder = self._patched_probe(
            monkeypatch,
            container_running=True,
            gh_results=[("", "executable file not found in $PATH", 127, False)],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-live"))

        assert result.state == "degraded"
        assert result.reason == "tool_missing"
        assert result.missing_tools == ("gh",)
        assert result.missing_env == ()

    def test_auth_missing_marks_degraded_with_missing_env(self, monkeypatch):
        gh_probe, _recorder = self._patched_probe(
            monkeypatch,
            container_running=True,
            gh_results=[("", "You are not logged into any GitHub hosts. Run gh auth login", 1, False)],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-live"))

        assert result.state == "degraded"
        assert result.reason == "env_missing"
        assert result.missing_env == ("GH_TOKEN",)
        assert result.missing_tools == ()

    def test_other_failure_marks_degraded_probe_failed_with_details(self, monkeypatch):
        gh_probe, _recorder = self._patched_probe(
            monkeypatch,
            container_running=True,
            gh_results=[("", "weird state", 7, False)],
        )

        result = asyncio.run(gh_probe.probe_v1("auto-live"))

        assert result.state == "degraded"
        assert result.reason == "probe_failed"
        assert result.details["exit_code"] == 7
        assert "weird state" in result.details["stderr"]


# ── WorktreeMonitor source_control composition ────────────────────────


class TestWorktreeMonitorCapabilityCache:
    """Refresh fans out source_control fetches for live rows; the result
    is cached and exposed via ``get_source_control``.
    """

    def _make_monitor(self, monkeypatch, *, rows, snapshots=None, exceptions=None):
        from tools.dashboard import worktree_monitor as wm_module

        snapshots = snapshots or {}
        exceptions = exceptions or {}

        async def fake_fetch(row, all_rows, *, watch_mode="silent"):
            key = (row.session_name, row.repo_name)
            if key in exceptions:
                raise exceptions[key]
            return snapshots.get(key, {
                "state": "ready",
                "implementation": "autonomy/github",
                "reason": None,
                "review": None,
                "watch": {"mode": watch_mode},
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

        # Use force_capabilities=True since the new background policy
        # never fetches silent rows by default. Operator-forced refresh
        # is the seeding path.
        asyncio.run(monitor.refresh(force_capabilities=True))

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
        asyncio.run(monitor.refresh(force_capabilities=True))
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
        # Force-refresh because the new policy gates silent rows.
        asyncio.run(monitor.refresh(force_capabilities=True))

        snapshot = monitor.get_source_control("auto-live", "autonomy")
        assert snapshot is not None
        assert snapshot["state"] == "degraded"
        assert snapshot["reason"] == "probe_failed"
        assert snapshot["review"] is None


class TestWorktreeMonitorBackgroundFetchPolicy:
    """Per Jeremy's directive (2026-04-30) the background poll must not
    do external network. Only exception: a watch-active row whose
    cached snapshot shows ``review.running == True`` AND poll budget
    remaining AND watch TTL elapsed. Operator-forced refresh bypasses
    every gate (subject only to the rate-limit backoff).

    Tests use the real clock with tiny TTLs / budgets — patching
    ``time.monotonic`` globally is unreliable because asyncio's event
    loop also reads it.
    """

    def _make_monitor(self, monkeypatch, *, rows, snapshot=None):
        from tools.dashboard import worktree_monitor as wm_module

        call_count = {"n": 0}
        snapshot = snapshot or {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": None,
            "watch": {"mode": "silent"},
        }

        async def fake_fetch(row, all_rows, *, watch_mode="silent"):
            call_count["n"] += 1
            # Echo through the watch_mode so the snapshot reflects the
            # row's current nag state.
            return {**snapshot, "watch": {"mode": watch_mode}}

        monkeypatch.setattr(wm_module, "_fetch_source_control", fake_fetch)
        monkeypatch.setattr(wm_module, "scan_all_worktrees", lambda: list(rows))
        return wm_module.WorktreeMonitor(), call_count

    def test_silent_row_never_fetches_in_background(self, monkeypatch):
        """No matter how many ticks, a silent row's background path
        does ZERO gh calls. Cards rely on operator-forced refresh or
        watch-active opt-in to populate."""
        rows = [_row(session="auto-live", live=True)]
        monitor, call_count = self._make_monitor(monkeypatch, rows=rows)

        for _ in range(5):
            asyncio.run(monitor.refresh())

        assert call_count["n"] == 0
        # Without a cached seed, no source_control block surfaces.
        assert monitor.get_source_control("auto-live", "autonomy") is None

    def test_force_capabilities_seeds_silent_row(self, monkeypatch):
        """An operator-forced refresh fetches even for silent rows —
        that's how the badge gets its first cached snapshot."""
        rows = [_row(session="auto-live", live=True)]
        monitor, call_count = self._make_monitor(monkeypatch, rows=rows)

        asyncio.run(monitor.refresh(force_capabilities=True))

        assert call_count["n"] == 1
        # Subsequent silent background ticks carry the snapshot
        # forward without re-fetching.
        asyncio.run(monitor.refresh())
        assert call_count["n"] == 1
        assert monitor.get_source_control("auto-live", "autonomy") is not None

    def test_watch_active_with_non_running_review_does_not_fetch(self, monkeypatch):
        """Watch-active rows still skip when cached.review.running is
        False — there's nothing transitioning to poll for."""
        rows = [_row(session="auto-live", live=True)]
        non_running_snapshot = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": {"running": False, "aggregate_state": "green", "checks": []},
            "watch": {"mode": "nag_all"},
        }
        monitor, call_count = self._make_monitor(
            monkeypatch, rows=rows, snapshot=non_running_snapshot,
        )
        monitor.set_nag_mode("auto-live", "autonomy", "nag_all")

        # Seed via force, then any number of background ticks must skip.
        asyncio.run(monitor.refresh(force_capabilities=True))
        seeded = call_count["n"]
        for _ in range(3):
            asyncio.run(monitor.refresh())

        assert call_count["n"] == seeded  # no additional fetches

    def test_watch_active_with_running_review_polls_in_background(
        self, monkeypatch,
    ):
        """The one allowed background-fetch path: watch-active row whose
        cached snapshot shows running checks. TTL gates frequency."""
        from tools.dashboard import worktree_monitor as wm_module
        # 1ms TTL so a tiny sleep lets the next background tick poll.
        monkeypatch.setattr(wm_module, "SOURCE_CONTROL_WATCH_TTL_SECONDS", 0.001)

        rows = [_row(session="auto-live", live=True)]
        running_snapshot = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": {"running": True, "aggregate_state": "green", "checks": []},
            "watch": {"mode": "nag_all"},
        }
        monitor, call_count = self._make_monitor(
            monkeypatch, rows=rows, snapshot=running_snapshot,
        )
        monitor.set_nag_mode("auto-live", "autonomy", "nag_all")

        # Seed via force.
        asyncio.run(monitor.refresh(force_capabilities=True))
        time.sleep(0.005)
        # Background tick: row is watch-active + running + TTL lapsed,
        # so it polls.
        asyncio.run(monitor.refresh())
        assert call_count["n"] >= 2

    def test_watch_active_running_blocked_by_poll_budget(self, monkeypatch):
        """Stuck-running PR can't drain quota indefinitely — the
        per-row hourly budget caps polling frequency."""
        from tools.dashboard import worktree_monitor as wm_module
        # 1ms TTL + 3-poll hourly cap. Anything past 3 polls in the
        # rolling hour stops fetching.
        monkeypatch.setattr(wm_module, "SOURCE_CONTROL_WATCH_TTL_SECONDS", 0.001)
        monkeypatch.setattr(wm_module, "MAX_POLLS_PER_HOUR_PER_ROW", 3)

        rows = [_row(session="auto-live", live=True)]
        running_snapshot = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": {"running": True, "aggregate_state": "green", "checks": []},
            "watch": {"mode": "nag_all"},
        }
        monitor, call_count = self._make_monitor(
            monkeypatch, rows=rows, snapshot=running_snapshot,
        )
        monitor.set_nag_mode("auto-live", "autonomy", "nag_all")

        # Seed (counts as one poll), then run many background ticks.
        asyncio.run(monitor.refresh(force_capabilities=True))
        for _ in range(10):
            time.sleep(0.002)
            asyncio.run(monitor.refresh())

        # Force seed = 1, plus at most 2 more background polls until
        # we hit the cap of 3 in the rolling window.
        assert call_count["n"] <= 3


class TestWorktreeMonitorRefreshOne:
    """``refresh_one`` is the operator-explicit force-GET path: scoped
    to one (session, repo). Bypasses TTL + poll budget for the target
    row, leaves all other rows alone, still respects rate-limit
    backoff so the operator can't accidentally deepen the hole.
    """

    def _make_monitor(self, monkeypatch, *, rows, snapshot=None):
        from tools.dashboard import worktree_monitor as wm_module

        snapshot = snapshot or {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": None,
            "watch": {"mode": "silent"},
        }
        captured = {"calls": []}

        async def fake_fetch(row, all_rows, *, watch_mode="silent"):
            captured["calls"].append((row.session_name, row.repo_name))
            return {**snapshot, "watch": {"mode": watch_mode}}

        monkeypatch.setattr(wm_module, "_fetch_source_control", fake_fetch)
        monkeypatch.setattr(wm_module, "scan_all_worktrees", lambda: list(rows))
        return wm_module.WorktreeMonitor(), captured

    def test_refresh_one_fetches_only_target_row(self, monkeypatch):
        rows = [
            _row(session="auto-target", live=True),
            _row(session="auto-other", live=True),
        ]
        monitor, captured = self._make_monitor(monkeypatch, rows=rows)

        asyncio.run(monitor.refresh_one("auto-target", "autonomy"))

        # Exactly one fetch — the target row. ``auto-other`` was
        # untouched (the whole point of the per-row endpoint).
        assert captured["calls"] == [("auto-target", "autonomy")]

    def test_refresh_one_bypasses_ttl(self, monkeypatch):
        from tools.dashboard import worktree_monitor as wm_module
        monkeypatch.setattr(wm_module, "SOURCE_CONTROL_WATCH_TTL_SECONDS", 60.0)

        rows = [_row(session="auto-x", live=True)]
        monitor, captured = self._make_monitor(monkeypatch, rows=rows)

        # Seed via background-allowed force, then do a per-row force —
        # second call must fetch even though we're well within the TTL.
        asyncio.run(monitor.refresh(force_capabilities=True))
        asyncio.run(monitor.refresh_one("auto-x", "autonomy"))

        assert len(captured["calls"]) == 2

    def test_refresh_one_respects_rate_limit_backoff(self, monkeypatch):
        from tools.dashboard import worktree_monitor as wm_module
        monkeypatch.setattr(wm_module, "RATE_LIMIT_BACKOFF_SECONDS", 60.0)

        rate_limited_snap = {
            "state": "degraded",
            "implementation": "autonomy/github",
            "reason": "rate_limited",
            "review": None,
            "watch": {"mode": "silent"},
        }
        rows = [_row(session="auto-x", live=True)]
        monitor, captured = self._make_monitor(
            monkeypatch, rows=rows, snapshot=rate_limited_snap,
        )

        # First call hits rate-limit -> arms backoff.
        asyncio.run(monitor.refresh_one("auto-x", "autonomy"))
        assert monitor._capability_backoff_until > 0.0
        assert len(captured["calls"]) == 1

        # Second call within the backoff window must NOT fetch — the
        # cached snapshot reflects the backoff.
        asyncio.run(monitor.refresh_one("auto-x", "autonomy"))
        assert len(captured["calls"]) == 1
        snap = monitor.get_source_control("auto-x", "autonomy")
        assert snap["reason"] == "rate_limited"
        assert "backoff_seconds_remaining" in (snap.get("details") or {})

    def test_refresh_one_clears_backoff_on_clean_response(self, monkeypatch):
        from tools.dashboard import worktree_monitor as wm_module
        monkeypatch.setattr(wm_module, "RATE_LIMIT_BACKOFF_SECONDS", 60.0)

        # Pre-arm the backoff to simulate "rate-limit recovered."
        rows = [_row(session="auto-x", live=True)]
        monitor, captured = self._make_monitor(monkeypatch, rows=rows)
        monitor._capability_backoff_until = time.monotonic() - 1.0  # already passed

        asyncio.run(monitor.refresh_one("auto-x", "autonomy"))

        # Clean response → backoff cleared.
        assert monitor._capability_backoff_until == 0.0


class TestWorktreeMonitorRateLimitBackoff:
    """When gh reports rate-limited, the monitor backs off all
    capability fetches for a window so we don't deepen the hole.
    Operator-forced refresh resets the back-off when the limit recovers.
    """

    def _make_monitor(self, monkeypatch, *, rows, responses):
        from tools.dashboard import worktree_monitor as wm_module

        idx = {"n": 0}

        async def fake_fetch(row, all_rows, *, watch_mode="silent"):
            i = idx["n"]
            idx["n"] += 1
            return responses[min(i, len(responses) - 1)]

        monkeypatch.setattr(wm_module, "_fetch_source_control", fake_fetch)
        monkeypatch.setattr(wm_module, "scan_all_worktrees", lambda: list(rows))
        return wm_module.WorktreeMonitor(), idx

    def test_rate_limited_response_arms_backoff_and_skips_subsequent(
        self, monkeypatch,
    ):
        from tools.dashboard import worktree_monitor as wm_module
        # Long backoff so the second refresh is comfortably inside it.
        monkeypatch.setattr(wm_module, "RATE_LIMIT_BACKOFF_SECONDS", 60.0)

        rate_limited = {
            "state": "degraded",
            "implementation": "autonomy/github",
            "reason": "rate_limited",
            "review": None,
            "watch": {"mode": "silent"},
            "details": {"error": "GitHub API rate limit exceeded"},
        }
        rows = [_row(session="auto-live", live=True)]
        monitor, idx = self._make_monitor(
            monkeypatch, rows=rows, responses=[rate_limited],
        )

        # Force the seed since silent rows don't fetch in background
        # under the new policy; rate-limit reply is what we want to
        # arm the back-off.
        asyncio.run(monitor.refresh(force_capabilities=True))
        # Subsequent unforced refresh is back-off-skipped.
        asyncio.run(monitor.refresh())

        # Only one actual fetch — the second was skipped due to back-off.
        assert idx["n"] == 1
        snap = monitor.get_source_control("auto-live", "autonomy")
        assert snap is not None
        assert snap["reason"] == "rate_limited"

    def test_force_capabilities_clears_backoff_on_clean_refresh(self, monkeypatch):
        from tools.dashboard import worktree_monitor as wm_module
        monkeypatch.setattr(wm_module, "RATE_LIMIT_BACKOFF_SECONDS", 60.0)

        rate_limited = {
            "state": "degraded",
            "implementation": "autonomy/github",
            "reason": "rate_limited",
            "review": None,
            "watch": {"mode": "silent"},
        }
        ready = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": None,
            "watch": {"mode": "silent"},
        }
        rows = [_row(session="auto-live", live=True)]
        monitor, idx = self._make_monitor(
            monkeypatch, rows=rows, responses=[rate_limited, ready],
        )
        # Force seed — rate-limited reply arms the back-off.
        asyncio.run(monitor.refresh(force_capabilities=True))
        assert monitor._capability_backoff_until > 0.0

        asyncio.run(monitor.refresh(force_capabilities=True))
        assert monitor._capability_backoff_until == 0.0
        snap = monitor.get_source_control("auto-live", "autonomy")
        assert snap["reason"] is None

    def test_rate_limited_synth_for_previously_uncached_row(self, monkeypatch):
        from tools.dashboard import worktree_monitor as wm_module
        monkeypatch.setattr(wm_module, "RATE_LIMIT_BACKOFF_SECONDS", 60.0)

        rate_limited = {
            "state": "degraded",
            "implementation": "autonomy/github",
            "reason": "rate_limited",
            "review": None,
            "watch": {"mode": "silent"},
        }

        rows_first = [_row(session="auto-old", live=True)]
        idx = {"n": 0}

        async def fake_fetch(row, all_rows, *, watch_mode="silent"):
            idx["n"] += 1
            return rate_limited

        monkeypatch.setattr(wm_module, "_fetch_source_control", fake_fetch)
        monkeypatch.setattr(wm_module, "scan_all_worktrees", lambda: list(rows_first))
        monitor = wm_module.WorktreeMonitor()

        # Force seed — rate-limited reply arms the back-off.
        asyncio.run(monitor.refresh(force_capabilities=True))
        # New row appears in the next scan — no cached snapshot for it.
        rows_second = [
            _row(session="auto-old", live=True),
            _row(session="auto-new", live=True),
        ]
        monkeypatch.setattr(wm_module, "scan_all_worktrees", lambda: list(rows_second))
        asyncio.run(monitor.refresh())

        # Still only one underlying fetch — second refresh was
        # back-off-skipped for both rows.
        assert idx["n"] == 1
        new_snap = monitor.get_source_control("auto-new", "autonomy")
        assert new_snap is not None
        assert new_snap["reason"] == "rate_limited"
        assert "backoff_seconds_remaining" in (new_snap.get("details") or {})


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


# ── Watch / nag mode persistence (P4) ─────────────────────────────────


class TestWorktreeMonitorNagMode:
    """``WorktreeMonitor`` persists the per-row nag mode in memory and
    exposes it on the cached source_control snapshot.
    """

    def test_default_nag_mode_is_silent(self):
        from tools.dashboard import worktree_monitor as wm_module

        monitor = wm_module.WorktreeMonitor()
        assert monitor.get_nag_mode("auto-x", "autonomy") == "silent"

    def test_set_nag_mode_persists_across_lookups(self):
        from tools.dashboard import worktree_monitor as wm_module

        monitor = wm_module.WorktreeMonitor()
        monitor.set_nag_mode("auto-x", "autonomy", "nag_all")
        assert monitor.get_nag_mode("auto-x", "autonomy") == "nag_all"
        # Distinct repo on the same session keeps its own default.
        assert monitor.get_nag_mode("auto-x", "enterprise") == "silent"

    def test_set_nag_mode_rejects_invalid_value(self):
        from tools.dashboard import worktree_monitor as wm_module

        monitor = wm_module.WorktreeMonitor()
        with pytest.raises(ValueError):
            monitor.set_nag_mode("auto-x", "autonomy", "loud")

    def test_set_nag_mode_updates_cached_snapshot_in_place(self):
        from tools.dashboard import worktree_monitor as wm_module

        monitor = wm_module.WorktreeMonitor()
        monitor._source_control_cache[("auto-x", "autonomy")] = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "review": None,
            "watch": {"mode": "silent"},
        }
        monitor.set_nag_mode("auto-x", "autonomy", "nag_done")
        cached = monitor.get_source_control("auto-x", "autonomy")
        # Cached watch block now carries both the mode and the
        # operator-visible expiry countdown — every nag request is
        # time-limited (Jeremy 2026-04-30).
        assert cached["watch"]["mode"] == "nag_done"
        assert cached["watch"]["expires_in_seconds"] > 0

    def test_nag_mode_auto_reverts_to_silent_after_expiry(self, monkeypatch):
        """Per Jeremy (2026-04-30): you can never request infinite nags.
        After the configured duration elapses, ``get_nag_mode`` returns
        silent without any explicit revert call.
        """
        from tools.dashboard import worktree_monitor as wm_module

        monitor = wm_module.WorktreeMonitor()
        # 1ms duration so we can step past it deterministically.
        monitor.set_nag_mode(
            "auto-x", "autonomy", "nag_all", duration_seconds=0.001,
        )
        assert monitor.get_nag_mode("auto-x", "autonomy") == "nag_all"
        time.sleep(0.005)
        # Past expiry — entry remains in-memory but the live mode is
        # silent so the polling decision tree won't poll any more.
        assert monitor.get_nag_mode("auto-x", "autonomy") == "silent"
        assert monitor.get_nag_expiry_remaining("auto-x", "autonomy") == 0.0

    def test_set_nag_mode_clamps_long_durations_to_max(self):
        from tools.dashboard import worktree_monitor as wm_module

        monitor = wm_module.WorktreeMonitor()
        # Try to ask for 100 hours — should be clamped to the 4-hour
        # ceiling (NAG_MAX_DURATION_SECONDS).
        monitor.set_nag_mode(
            "auto-x", "autonomy", "nag_all", duration_seconds=360_000.0,
        )
        remaining = monitor.get_nag_expiry_remaining("auto-x", "autonomy")
        assert remaining <= wm_module.NAG_MAX_DURATION_SECONDS

    def test_set_nag_mode_rejects_zero_or_negative_duration(self):
        from tools.dashboard import worktree_monitor as wm_module

        monitor = wm_module.WorktreeMonitor()
        with pytest.raises(ValueError):
            monitor.set_nag_mode(
                "auto-x", "autonomy", "nag_all", duration_seconds=0.0,
            )
        with pytest.raises(ValueError):
            monitor.set_nag_mode(
                "auto-x", "autonomy", "nag_all", duration_seconds=-5.0,
            )


class TestWorktreeWatchEndpoint:
    """``PUT /api/worktrees/{session}/{repo}/watch`` writes the nag mode."""

    def test_put_watch_sets_mode(self, test_client, monkeypatch):
        from tools.dashboard import server

        captured = {}

        def fake_set(session, repo, mode, *, duration_seconds=None):
            captured["args"] = (session, repo, mode)
            captured["duration_seconds"] = duration_seconds
            return mode

        monkeypatch.setattr(server.worktree_monitor, "set_nag_mode", fake_set)
        monkeypatch.setattr(
            server.worktree_monitor, "get_nag_expiry_remaining",
            lambda *_: 3600.0,
        )

        resp = test_client.put(
            "/api/worktrees/auto-x/autonomy/watch",
            json={"mode": "nag_all"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "ok": True,
            "session_name": "auto-x",
            "repo_name": "autonomy",
            "mode": "nag_all",
            "expires_in_seconds": 3600,
        }
        assert captured["args"] == ("auto-x", "autonomy", "nag_all")
        # No explicit duration passed in body -> None reaches set_nag_mode,
        # which then applies its own NAG_DEFAULT_DURATION_SECONDS.
        assert captured["duration_seconds"] is None

    def test_put_watch_accepts_explicit_duration(self, test_client, monkeypatch):
        """Body can include ``duration_seconds`` to override the default."""
        from tools.dashboard import server

        captured = {}

        def fake_set(session, repo, mode, *, duration_seconds=None):
            captured["duration_seconds"] = duration_seconds
            return mode

        monkeypatch.setattr(server.worktree_monitor, "set_nag_mode", fake_set)
        monkeypatch.setattr(
            server.worktree_monitor, "get_nag_expiry_remaining",
            lambda *_: 600.0,
        )

        resp = test_client.put(
            "/api/worktrees/auto-x/autonomy/watch",
            json={"mode": "nag_done", "duration_seconds": 600},
        )

        assert resp.status_code == 200
        assert captured["duration_seconds"] == 600
        assert resp.json()["expires_in_seconds"] == 600

    def test_put_watch_rejects_unknown_mode(self, test_client, monkeypatch):
        from tools.dashboard import server

        called = {"set": False}

        def fake_set(*_):
            called["set"] = True
            raise AssertionError("set_nag_mode should not run on invalid mode")

        monkeypatch.setattr(server.worktree_monitor, "set_nag_mode", fake_set)

        resp = test_client.put(
            "/api/worktrees/auto-x/autonomy/watch",
            json={"mode": "loud"},
        )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid mode"
        assert called["set"] is False

    def test_put_watch_defaults_missing_mode_to_silent(self, test_client, monkeypatch):
        from tools.dashboard import server

        captured = {}

        def fake_set(session, repo, mode, *, duration_seconds=None):
            captured["args"] = (session, repo, mode)
            return mode

        monkeypatch.setattr(server.worktree_monitor, "set_nag_mode", fake_set)
        monkeypatch.setattr(
            server.worktree_monitor, "get_nag_expiry_remaining", lambda *_: 0.0,
        )

        resp = test_client.put("/api/worktrees/auto-x/autonomy/watch", json={})

        assert resp.status_code == 200
        assert captured["args"] == ("auto-x", "autonomy", "silent")


# ── Operator-declared review bindings (auto-nrqbs) ─────────────────────


@pytest.fixture
def isolated_settings_db(monkeypatch, tmp_path):
    """Pin Settings to a per-test SQLite file so binding/cache writes
    don't bleed across tests or touch the operator's real DB."""
    db_path = tmp_path / "settings.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    yield db_path


class TestWorktreeReviewBindings:
    """Resolver wiring: bindings drive composition; absence falls back to legacy."""

    def test_no_bindings_falls_back_to_legacy_auto_detect(
        self, isolated_settings_db, monkeypatch,
    ):
        """When no binding exists, the resolver still runs the legacy
        ``gh pr list`` auto-detect path. Behavior unchanged for unbound rows.
        """
        from agents.capabilities.github import probe as github_probe
        from agents.capabilities.github import service as wg
        from tools.dashboard import worktree_monitor as wm

        async def fake_probe(_session, *, timeout=3):
            return github_probe.ProbeResult(state=github_probe.STATE_READY, reason=None)

        async def fake_review_read(*args, **kwargs):
            return wg.WorktreeGithubExecResult(
                operation=wg.OP_REVIEW_READ,
                session_name=kwargs.get("session_name") or "auto-x",
                repo_name="autonomy",
                ok=True,
                stdout='[{"number": 7, "title": "Legacy auto-detected", '
                       '"body": "", "state": "OPEN", "headRefName": "session/auto-x", '
                       '"baseRefName": "main", "isDraft": false, '
                       '"statusCheckRollup": [], "commits": []}]',
            )

        monkeypatch.setattr(github_probe, "probe_v1", fake_probe)
        monkeypatch.setattr(wm, "source_control_review_read_v1", fake_review_read)

        row = _row(session="auto-x", repo="autonomy", live=True)
        snapshot = asyncio.run(wm._fetch_source_control(row, [row]))
        assert snapshot["state"] == "ready"
        # Legacy single-PR review wraps as a 1-item plural list.
        assert len(snapshot["reviews"]) == 1
        assert snapshot["reviews"][0]["number"] == 7
        # Back-compat alias also present.
        assert snapshot["review"]["number"] == 7

    def test_binding_present_composes_from_cache_with_zero_gh_calls(
        self, isolated_settings_db, monkeypatch,
    ):
        """When the operator declared a binding and the cache is hot,
        the snapshot is composed from the Setting rows alone — no
        docker exec / gh fan-out."""
        from tools.graph import settings_ops
        from tools.dashboard import worktree_monitor as wm
        from agents.capabilities.github import service as wg

        # Seed binding + cache.
        settings_ops.add_setting(
            "autonomy.worktree.review_binding", 1,
            "auto-x:autonomy:session/auto-x:42",
            {"base_sha": "fa12cd34"},
            org="autonomy",
        )
        settings_ops.add_setting(
            "autonomy.source_control.review_state", 1,
            "owner/repo:42",
            {
                "title": "Cached PR", "body": "from cache",
                "state": "open", "head_sha": "head456", "base_sha": "real-base",
                "base_branch": "main", "is_draft": False, "provider": "github",
                "url": "https://example/pull/42",
                "checks": [
                    {"id": "build", "label": "build", "status": "pass"},
                    {"id": "test",  "label": "test",  "status": "running"},
                ],
            },
            org="autonomy",
        )

        # If anything reaches gh, the test fails loudly.
        async def boom(*args, **kwargs):
            raise AssertionError("legacy auto-detect should not run when binding exists")

        monkeypatch.setattr(wm, "source_control_review_read_v1", boom)
        # ``worktree_monitor`` imported derive_repo_slug at module load,
        # so patch the already-bound name on the consumer module rather
        # than the source — patching wg.derive_repo_slug would no-op.
        monkeypatch.setattr(wm, "derive_repo_slug", lambda _p: "owner/repo")

        row = _row(session="auto-x", repo="autonomy", live=True)
        snapshot = asyncio.run(wm._fetch_source_control(row, [row]))
        assert snapshot["state"] == "ready"
        assert len(snapshot["reviews"]) == 1
        review = snapshot["reviews"][0]
        assert review["number"] == 42
        assert review["title"] == "Cached PR"
        # Binding base_sha overrides cache.base_sha for per-PR scoping.
        assert review["base_sha"] == "fa12cd34"
        # Running disc bubbles up from the cached check entries.
        assert review["running"] is True

    def test_stacked_pr_bindings_compose_into_ordered_list(
        self, isolated_settings_db, monkeypatch,
    ):
        """Two bindings on one branch produce a 2-item ``reviews`` list."""
        from tools.graph import settings_ops
        from tools.dashboard import worktree_monitor as wm
        from agents.capabilities.github import service as wg

        for review_id, base_sha in [("100", "ba100abc"), ("101", "ba101abc")]:
            settings_ops.add_setting(
                "autonomy.worktree.review_binding", 1,
                f"auto-x:autonomy:session/auto-x:{review_id}",
                {"base_sha": base_sha},
                org="autonomy",
            )
            settings_ops.add_setting(
                "autonomy.source_control.review_state", 1,
                f"owner/repo:{review_id}",
                {
                    "title": f"Stack #{review_id}", "body": "",
                    "state": "open", "head_sha": f"head{review_id}",
                    "base_sha": "ignored-by-resolver", "base_branch": "main",
                    "is_draft": False, "provider": "github",
                },
                org="autonomy",
            )

        monkeypatch.setattr(wm, "derive_repo_slug", lambda _p: "owner/repo")
        row = _row(session="auto-x", repo="autonomy", live=True)
        snapshot = asyncio.run(wm._fetch_source_control(row, [row]))
        ids = sorted(r["review_id"] for r in snapshot["reviews"])
        assert ids == ["100", "101"]

    def test_binding_without_cache_marks_review_stale(
        self, isolated_settings_db, monkeypatch,
    ):
        """An operator declared the binding but no fetch has populated
        the cache yet — surface a stub flagged ``stale: True`` so the
        UI nudges toward Refresh."""
        from tools.graph import settings_ops
        from tools.dashboard import worktree_monitor as wm

        settings_ops.add_setting(
            "autonomy.worktree.review_binding", 1,
            "auto-x:autonomy:session/auto-x:9",
            {"base_sha": "abc123"},
            org="autonomy",
        )
        monkeypatch.setattr(wm, "derive_repo_slug", lambda _p: "owner/repo")

        row = _row(session="auto-x", repo="autonomy", live=True)
        snapshot = asyncio.run(wm._fetch_source_control(row, [row]))
        assert snapshot["state"] == "ready"
        assert snapshot.get("stale") is True
        assert snapshot["reviews"][0]["stale"] is True

    def test_cleanup_deletes_bindings_keeps_review_state_cache(
        self, isolated_settings_db,
    ):
        """``cleanup_session_worktrees`` wipes bindings for the session;
        the per-org review_state cache survives so the next worktree
        targeting the same review re-uses it."""
        from tools.graph import settings_ops
        from agents import workspace_manager

        # Seed: one binding for the session, one cache row for the same
        # review id (different setting key prefix).
        settings_ops.add_setting(
            "autonomy.worktree.review_binding", 1,
            "auto-doomed:autonomy:session/auto-doomed:42",
            {"base_sha": "abc"},
            org="autonomy",
        )
        settings_ops.add_setting(
            "autonomy.source_control.review_state", 1,
            "owner/repo:42",
            {
                "title": "T", "body": "", "state": "open",
                "head_sha": "h", "base_sha": "b", "base_branch": "main",
                "is_draft": False, "provider": "github",
            },
            org="autonomy",
        )

        # Helper directly — exercising the same path cleanup_session_worktrees
        # invokes after the worktree directory is removed.
        deleted = workspace_manager._delete_review_bindings_for_session("auto-doomed")
        assert deleted == 1

        binding_rows = settings_ops.read_set(
            "autonomy.worktree.review_binding",
            prefix="auto-doomed",
            org="autonomy",
        )
        assert list(binding_rows.members) == []

        cache_rows = settings_ops.read_set(
            "autonomy.source_control.review_state",
            prefix="owner/repo",
            org="autonomy",
        ).to_dict()
        assert "owner/repo:42" in cache_rows

    def test_pr_diff_endpoint_uses_binding_base_sha_when_present(
        self, test_client, isolated_settings_db, monkeypatch,
    ):
        """``GET /api/worktrees/.../pr-diff`` scopes the diff to
        ``binding.base_sha..cache.head_sha`` when a binding exists."""
        from tools.dashboard import server
        from tools.graph import settings_ops

        # Prime the worktree monitor's snapshot cache directly so the
        # diff resolver reads our review object — no probe required.
        snapshot = {
            "state": "ready",
            "implementation": "autonomy/github",
            "reason": None,
            "reviews": [
                {
                    "number": 11, "review_id": "11", "url": "",
                    "title": "Bound PR", "body": "",
                    "head_sha": "headSHA", "base_sha": "baseSHA",
                    "state": "open", "is_draft": False,
                    "aggregate_state": "green", "running": False, "checks": [],
                },
            ],
            "review": None,
            "watch": {"mode": "silent"},
        }
        server.worktree_monitor._source_control_cache[("auto-x", "autonomy")] = snapshot

        captured: dict = {}

        def fake_detail(session_name, repo_name, *, base_sha=None, head_sha=None):
            captured["base"] = base_sha
            captured["head"] = head_sha
            return server.WorktreeDirtyDetail(files=[], patch="")

        monkeypatch.setattr(server, "get_session_worktree_integrated_diff", fake_detail)
        # The /api/worktrees stub bypass — we don't need monitor.refresh.
        monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: [])

        resp = test_client.get("/api/worktrees/auto-x/autonomy/pr-diff")
        assert resp.status_code == 200
        assert captured["base"] == "baseSHA"
        assert captured["head"] == "headSHA"
        # cleanup
        server.worktree_monitor._source_control_cache.pop(("auto-x", "autonomy"), None)

    def test_pr_diff_endpoint_returns_stale_for_orphaned_shas(
        self, test_client, isolated_settings_db, monkeypatch, tmp_path,
    ):
        """When the cached head_sha doesn't exist locally (force-push
        orphaned it), the response carries ``stale: true`` instead
        of crashing."""
        from tools.dashboard import server
        from agents import workspace_manager

        # Real worktree path that exists but has no commits — git
        # cat-file on a fake sha returns rc=1, exercising the stale path.
        worktree_dir = tmp_path / "worktrees" / "auto-y" / "autonomy"
        worktree_dir.mkdir(parents=True)
        # Init a real (empty) git repo so cat-file actually runs.
        import subprocess
        subprocess.run(["git", "init", "-q", str(worktree_dir)], check=True)

        snapshot = {
            "state": "ready",
            "reviews": [{
                "number": 12, "review_id": "12",
                "head_sha": "deadbeefdeadbeef", "base_sha": "facefacefaceface",
            }],
            "review": None,
            "watch": {"mode": "silent"},
        }
        server.worktree_monitor._source_control_cache[("auto-y", "autonomy")] = snapshot
        monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: [])

        # Patch the integrated_diff entry-point's directory resolution to
        # land in our scratch worktree.
        def fake_detail(session, repo, *, base_sha=None, head_sha=None):
            return workspace_manager.get_session_worktree_integrated_diff(
                session, repo,
                base_sha=base_sha, head_sha=head_sha,
                worktrees_dir=tmp_path / "worktrees",
            )

        monkeypatch.setattr(server, "get_session_worktree_integrated_diff", fake_detail)

        resp = test_client.get("/api/worktrees/auto-y/autonomy/pr-diff")
        assert resp.status_code == 200
        body = resp.json()
        assert body["stale"] is True
        assert "reason" in body
        server.worktree_monitor._source_control_cache.pop(("auto-y", "autonomy"), None)


class TestRefreshOneOverBindings:
    """``refresh_one`` walks bindings + REST when present."""

    def _seed_binding(self):
        from tools.graph import settings_ops
        settings_ops.add_setting(
            "autonomy.worktree.review_binding", 1,
            "auto-x:autonomy:session/auto-x:99",
            {"base_sha": "fa9bcd"},
            org="autonomy",
        )

    def test_refresh_one_binding_path_writes_cache_and_recomposes(
        self, isolated_settings_db, monkeypatch,
    ):
        from agents.capabilities.github import service as wg
        from tools.dashboard import worktree_monitor as wm
        from tools.graph import settings_ops

        self._seed_binding()

        # Stub the REST review fetch + check-runs fetch.
        async def fake_review(*args, **kwargs):
            return wg.WorktreeGithubExecResult(
                operation=wg.OP_REVIEW_READ_BY_ID,
                session_name="auto-x", repo_name="autonomy",
                ok=True,
                stdout=(
                    'HTTP/2.0 200 OK\r\nETag: "etag-A"\r\n\r\n'
                    '{"title":"Bound","body":"B","state":"open",'
                    '"draft":false,"html_url":"https://example/pull/99",'
                    '"head":{"sha":"headSHA"},'
                    '"base":{"sha":"baseSHA","ref":"main"}}'
                ),
            )

        async def fake_checks(*args, **kwargs):
            return wg.WorktreeGithubExecResult(
                operation=wg.OP_CHECK_RUNS_READ_FOR_SHA,
                session_name="auto-x", repo_name="autonomy",
                ok=True,
                stdout='{"check_runs":[{"id":1,"name":"build","status":"completed","conclusion":"success"}]}',
            )

        monkeypatch.setattr(wm, "source_control_review_read_by_id_v1", fake_review)
        monkeypatch.setattr(wm, "source_control_check_runs_read_for_sha_v1", fake_checks)
        monkeypatch.setattr(wm, "derive_repo_slug", lambda _p: "owner/repo")

        # Use a fresh monitor so global state doesn't leak across tests.
        monitor = wm.WorktreeMonitor()
        row = _row(session="auto-x", repo="autonomy", live=True)
        # Drive _refresh_one_source_control synchronously via asyncio.run.
        asyncio.run(monitor._refresh_one_source_control(row, [row]))

        # Cache row landed.
        cached = settings_ops.read_set(
            "autonomy.source_control.review_state",
            prefix="owner/repo", org="autonomy",
        ).to_dict()
        assert "owner/repo:99" in cached
        payload = cached["owner/repo:99"].payload
        assert payload["title"] == "Bound"
        assert payload["head_sha"] == "headSHA"
        assert payload["etag"] == '"etag-A"'
        assert payload["checks"][0]["status"] == "pass"

        # Snapshot recomposes from the freshly written cache.
        snapshot = monitor.get_source_control("auto-x", "autonomy")
        assert snapshot is not None
        assert snapshot["reviews"][0]["title"] == "Bound"

    def test_refresh_one_binding_path_treats_304_as_cache_fresh(
        self, isolated_settings_db, monkeypatch,
    ):
        from agents.capabilities.github import service as wg
        from tools.dashboard import worktree_monitor as wm
        from tools.graph import settings_ops

        self._seed_binding()
        # Pre-seed cache with an etag.
        settings_ops.add_setting(
            "autonomy.source_control.review_state", 1,
            "owner/repo:99",
            {
                "title": "Cached", "body": "B", "state": "open",
                "head_sha": "headSHA", "base_sha": "baseSHA",
                "base_branch": "main", "is_draft": False, "provider": "github",
                "etag": '"etag-A"',
                "checks": [{"id": "build", "label": "build", "status": "pass"}],
            },
            org="autonomy",
        )

        async def fake_review(*args, **kwargs):
            return wg.WorktreeGithubExecResult(
                operation=wg.OP_REVIEW_READ_BY_ID,
                session_name="auto-x", repo_name="autonomy",
                ok=False,
                failure=wg.FAILURE_NOT_MODIFIED,
                stdout="HTTP/2.0 304 Not Modified\r\n\r\n",
            )

        # check-runs op should NOT be called on 304 — assert if it is.
        async def boom_checks(*args, **kwargs):
            raise AssertionError("check-runs should not be re-fetched on 304")

        monkeypatch.setattr(wm, "source_control_review_read_by_id_v1", fake_review)
        monkeypatch.setattr(wm, "source_control_check_runs_read_for_sha_v1", boom_checks)
        monkeypatch.setattr(wm, "derive_repo_slug", lambda _p: "owner/repo")

        monitor = wm.WorktreeMonitor()
        row = _row(session="auto-x", repo="autonomy", live=True)
        asyncio.run(monitor._refresh_one_source_control(row, [row]))

        # Cache content unchanged; the snapshot still reads "Cached".
        snapshot = monitor.get_source_control("auto-x", "autonomy")
        assert snapshot["reviews"][0]["title"] == "Cached"
