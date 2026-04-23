"""HTTP and wiring tests for the Worktrees dashboard page."""

from pathlib import Path

from agents.workspace_manager import CleanupResult, WorktreeState, WorkspaceError


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
):
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
    )


def _install_fake_monitor(monkeypatch, rows):
    from tools.dashboard import server

    fake = _FakeMonitor(rows)
    monkeypatch.setattr(server.worktree_monitor, "get_all", fake.get_all)
    monkeypatch.setattr(server.worktree_monitor, "refresh", fake.refresh)
    return server, fake


class TestWorktreeAPI:
    def test_get_worktrees_serializes_cached_rows(self, test_client, monkeypatch):
        _server, _fake = _install_fake_monitor(monkeypatch, [_row()])

        resp = test_client.get("/api/worktrees")

        assert resp.status_code == 200
        data = resp.json()
        assert data == [{
            "session_name": "auto-test",
            "repo_name": "autonomy",
            "worktree_path": "/tmp/worktrees/auto-test/autonomy",
            "managed_clone": "/tmp/repos/autonomy.git",
            "branch": "session/auto-test",
            "commits_ahead": 1,
            "is_dirty": False,
            "ff_eligible": True,
            "session_live": False,
        }]

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
        assert 'data-testid="worktree-merge-button"' in html
        assert 'data-testid="worktree-cleanup-button"' in html

    def test_static_js_wires_polling_and_actions(self):
        js = (JS_DIR / "pages" / "worktrees.js").read_text()
        assert "fetch('/api/worktrees')" in js
        assert "'/api/worktrees/' + encodeURIComponent(row.session_name)" in js
        assert "row.repo_name === 'autonomy' && row.ff_eligible" in js
        assert "setInterval(() => this.refresh(false), 30000)" in js
        assert "window.showToast" in js

    def test_spa_router_knows_worktrees_route(self):
        app_js = (JS_DIR.parent / "app.js").read_text()
        assert "renderWorktreesFragment" in app_js
        assert "fetch('/pages/worktrees')" in app_js
        assert "path === '/worktrees'" in app_js

    def test_template_uses_required_status_labels(self):
        template = (TEMPLATE_DIR / "pages" / "worktrees.html").read_text()
        assert "LIVE" in (JS_DIR / "pages" / "worktrees.js").read_text()
        assert "ORPHANED" in (JS_DIR / "pages" / "worktrees.js").read_text()
        assert "DEAD-CLEAN" in (JS_DIR / "pages" / "worktrees.js").read_text()
        assert "Session" in template
        assert "Repo" in template
        assert "Branch" in template
        assert "Ahead" in template
