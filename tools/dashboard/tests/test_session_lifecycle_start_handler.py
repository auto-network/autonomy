from types import SimpleNamespace

import pytest

from tools.dashboard.dao import dashboard_db
from tools.dashboard.session_lifecycle_worker import LifecycleJob, SessionLifecycleStateWriter


@pytest.fixture(autouse=True)
def _close_dashboard_db_after_test():
    yield
    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None


def _init_db(tmp_path):
    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None
    dashboard_db.init_db(tmp_path / "dashboard.db")


def _project():
    return SimpleNamespace(
        id="blindhash-operations",
        name="BlindHash Operations",
        graph_project="blindhash",
        default_tags=[],
        env={},
        env_from_host=[],
        startup=None,
        working_dir="/workspace/repo",
        image="autonomy-agent:dashboard",
        harness="claude",
        model=None,
        dind=False,
        network_host=False,
        capabilities=[],
    )


def test_project_start_handler_prepares_launches_registers_without_event_loop(monkeypatch, tmp_path):
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="auto-life",
        session_type="container",
        project="blindhash-operations",
        harness="claude",
    )
    proj = _project()
    calls = {"tmux": [], "prepare": []}

    monkeypatch.setattr(server, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _project_id: proj)
    monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _proj: {})
    monkeypatch.setattr(server, "render_workspace_primer", lambda _proj: "primer")

    def fake_prepare(workspace, tmux_name, **kwargs):
        calls["prepare"].append((workspace.id, tmux_name, kwargs))
        return {str(tmp_path / "repo"): "/workspace/repo"}

    def fake_launch_session(**kwargs):
        assert kwargs["name"] == "auto-life"
        assert kwargs["metadata"]["project"] == "blindhash-operations"
        assert kwargs["mounts"] == {str(tmp_path / "repo"): "/workspace/repo"}
        return "echo launched"

    def fake_subprocess_run(cmd, **kwargs):
        calls["tmux"].append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(server, "prepare_session_mounts", fake_prepare)
    monkeypatch.setattr(server, "launch_session", fake_launch_session)
    monkeypatch.setattr(server.subprocess, "run", fake_subprocess_run)

    server._run_project_session_start(
        LifecycleJob("start", "auto-life", {"project_id": "blindhash-operations"}),
        SessionLifecycleStateWriter(),
    )

    row = dashboard_db.get_session("auto-life")
    assert row is not None
    assert row["startup_state"] == "harness_starting"
    assert row["activity_state"] == "running"
    assert row["is_live"] == 1
    assert row["project"] == "blindhash-operations"
    assert row["resolution_dir"].endswith("/sessions")
    assert row["last_message"] == "Starting..."
    assert row["lifecycle_detail"] is None
    assert calls["prepare"][0][2]["git_timeout"] == 120
    assert calls["prepare"][0][2]["refresh_existing_worktree"] is True
    assert calls["tmux"][0][0][:6] == [
        "tmux", "new-session", "-d", "-s", "auto-life", "-x",
    ]
    assert all("timeout" in kwargs for _cmd, kwargs in calls["tmux"])


def test_project_start_handler_failure_writes_lifecycle_detail(monkeypatch, tmp_path):
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="auto-life",
        session_type="container",
        project="blindhash-operations",
        harness="claude",
    )

    monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _project_id: _project())
    monkeypatch.setattr(server, "prepare_session_mounts", lambda *_a, **_kw: {})
    monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _proj: {})
    monkeypatch.setattr(server, "render_workspace_primer", lambda _proj: "primer")
    monkeypatch.setattr(server, "launch_session", lambda **_kw: None)

    server._run_project_session_start(
        LifecycleJob("start", "auto-life", {"project_id": "blindhash-operations"}),
        SessionLifecycleStateWriter(),
    )

    row = dashboard_db.get_session("auto-life")
    assert row is not None
    assert row["startup_state"] == "setup_failed"
    assert row["activity_state"] == "failed"
    assert row["is_live"] == 0
    assert "launch_session failed" in row["lifecycle_detail"]
