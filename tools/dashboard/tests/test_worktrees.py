"""HTTP and wiring tests for the Worktrees dashboard page."""

from pathlib import Path

from agents.workspace_manager import (
    CleanupResult,
    GitFileChange,
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
    def test_count_worktrees_counts_dirty_only_rows_separately(self, monkeypatch):
        from tools.dashboard import server

        rows = [
            _row(session="auto-commit", ahead=1, dirty=True),
            _row(session="auto-dirty", ahead=0, dirty=True, commits=[]),
            _row(session="auto-clean", ahead=0, dirty=False, commits=[]),
        ]
        monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: list(rows))

        assert server._count_worktrees() == {"with_commits": 1, "with_changes": 1}

    def test_get_worktrees_serializes_cached_rows(self, test_client, monkeypatch):
        _server, _fake = _install_fake_monitor(monkeypatch, [_row()])

        resp = test_client.get("/api/worktrees")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["session_name"] == "auto-test"
        assert row["repo_name"] == "autonomy"
        assert row["worktree_path"] == "/tmp/worktrees/auto-test/autonomy"
        assert row["managed_clone"] == "/tmp/repos/autonomy.git"
        assert row["branch"] == "session/auto-test"
        assert row["target_branch"] in {"main", "master"}
        assert row["commits_ahead"] == 1
        assert row["is_dirty"] is False
        assert row["ff_eligible"] is True
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

    def test_cleanup_endpoint_calls_workspace_cleanup_and_refreshes(self, test_client, monkeypatch):
        server, fake = _install_fake_monitor(monkeypatch, [_row()])
        called = {}

        def fake_cleanup(session_name, *, force=False):
            called["args"] = (session_name, force)
            return CleanupResult(
                removed=["/tmp/worktrees/auto-test/autonomy"],
                preserved=[("/tmp/worktrees/auto-test/enterprise", "unpushed commits")],
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
                    "reason": "unpushed commits",
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
        assert "'/api/worktrees/' + encodeURIComponent(row.session_name)" in js
        assert "canDiscardDirtyRow(row)" in js
        assert "fitPath(path, el)" in js
        assert "repoName(row)" in js
        assert "row.repo_name === 'autonomy'" in js
        assert "'/commits/'" in js
        assert "'/merge'" in js
        assert "'/changes'" in js
        assert "'/discard'" in js
        assert "setInterval(() => {" in js
        assert "window.showToast" in js

    def test_spa_router_knows_worktrees_route(self):
        app_js = (JS_DIR.parent / "app.js").read_text()
        assert "renderWorktreesFragment" in app_js
        assert "fetch('/pages/worktrees')" in app_js
        assert "path === '/worktrees'" in app_js

    def test_template_uses_required_status_labels(self):
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        js = (JS_DIR / "pages" / "worktrees.js").read_text()
        assert "LIVE" in js
        assert "ORPHANED" in js
        assert "DEAD-CLEAN" in js
        assert "Worktrees" in template
        assert "Commits" in template
        assert "Changes" in template
        assert 'x-markdown="selectedCommit.commit.body"' in template
        assert 'x-text="fitPath(file.path, $el)"' in template
        assert 'x-text="repoName(item.row)"' in template
        assert "changesCompanionCommitLabel(row)" in template
        assert "1 commit also present" in js
        assert 'x-show="canDiscardDirtyRow(row)"' in template
        assert "Are you sure you want to delete this Worktree?" in template
