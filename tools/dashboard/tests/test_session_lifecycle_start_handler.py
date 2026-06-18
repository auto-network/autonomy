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
        capabilities=("capability-one",),
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
        assert kwargs["capabilities"] == proj.capabilities
        return "echo launched"

    def fake_subprocess_run(cmd, **kwargs):
        calls["tmux"].append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(server, "prepare_session_mounts", fake_prepare)
    monkeypatch.setattr(server, "launch_session", fake_launch_session)
    monkeypatch.setattr(server.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(server, "_wait_for_setup_complete", lambda **_kwargs: None)
    monkeypatch.setattr(server, "_wait_for_prompt", lambda **_kwargs: None)
    monkeypatch.setattr(server, "_render_worker_first_message", lambda **_kwargs: ("Hello", False))
    monkeypatch.setattr(server, "_inject_echo_verified", lambda **_kwargs: None)

    server._run_project_session_start(
        LifecycleJob("start", "auto-life", {"project_id": "blindhash-operations"}),
        SessionLifecycleStateWriter(),
    )

    row = dashboard_db.get_session("auto-life")
    assert row is not None
    assert row["startup_state"] is None
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
    cleanup_calls = []
    monkeypatch.setattr(
        server,
        "_cleanup_after_lifecycle_failure",
        lambda **kwargs: cleanup_calls.append(kwargs) or [],
    )

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
    assert cleanup_calls and cleanup_calls[0]["tmux_name"] == "auto-life"


def test_project_start_handler_cleanup_error_preserves_failed_state(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        server,
        "_cleanup_after_lifecycle_failure",
        lambda **_kwargs: ["stop_container_tmux failed"],
    )

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
    assert "cleanup errors" in row["lifecycle_detail"]


def test_inject_echo_verified_pastes_before_enter(monkeypatch):
    from tools.dashboard import server

    calls = []

    def fake_capture(tmux_name, *, timeout=None):
        calls.append(("capture", tmux_name, timeout))
        if any(call[0] == "paste" for call in calls):
            return "> Lifecycle hello"
        return "> "

    def fake_paste(tmux_name, message, *, timeout):
        calls.append(("paste", tmux_name, message, timeout))

    def fake_enter(tmux_name, *, timeout):
        calls.append(("enter", tmux_name, timeout))

    monkeypatch.setattr(server, "_run_tmux_capture", fake_capture)
    monkeypatch.setattr(server, "tmux_paste_checked_sync", fake_paste)
    monkeypatch.setattr(server, "tmux_enter_checked_sync", fake_enter)

    server._inject_echo_verified(
        tmux_name="auto-life",
        message="Lifecycle hello",
        harness_name="claude",
        deadline=server.time.monotonic() + 30,
    )

    assert [call[0] for call in calls] == ["capture", "paste", "capture", "enter"]


def test_wait_for_prompt_requires_composer_ready(monkeypatch, tmp_path):
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="auto-life",
        session_type="container",
        project="blindhash-operations",
        harness="claude",
    )
    captures = iter(["loading", "> "])

    class Harness:
        def read_screen_state(self, pane_text, current_state):
            return (
                {
                    "composer_ready": pane_text == "> ",
                    "confirming_trust_prompt": False,
                    "blocking_modal": None,
                },
                [],
            )

    monkeypatch.setattr(server, "get_session_harness", lambda _name: Harness())
    monkeypatch.setattr(server, "_run_tmux_capture", lambda *_a, **_kw: next(captures))
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)

    server._wait_for_prompt(
        tmux_name="auto-life",
        harness_name="claude",
        deadline=server.time.monotonic() + 30,
    )

    row = dashboard_db.get_session("auto-life")
    assert '"composer_ready": true' in row["harness_state"]


def test_wait_for_prompt_does_not_return_while_confirming_trust(monkeypatch, tmp_path):
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="auto-life",
        session_type="container",
        project="blindhash-operations",
        harness="claude",
    )
    captures = iter(["trust", "> "])
    keys_sent = []

    class Harness:
        def read_screen_state(self, pane_text, current_state):
            if pane_text == "trust":
                return (
                    {
                        "composer_ready": True,
                        "confirming_trust_prompt": True,
                        "blocking_modal": None,
                    },
                    [{"kind": "key", "value": "C-m"}],
                )
            return (
                {
                    "composer_ready": True,
                    "confirming_trust_prompt": False,
                    "blocking_modal": None,
                },
                [],
            )

    monkeypatch.setattr(server, "get_session_harness", lambda _name: Harness())
    monkeypatch.setattr(server, "_run_tmux_capture", lambda *_a, **_kw: next(captures))
    monkeypatch.setattr(
        server,
        "_run_tmux_keystrokes",
        lambda _tmux_name, keystrokes, **_kw: keys_sent.extend(keystrokes),
    )
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)

    server._wait_for_prompt(
        tmux_name="auto-life",
        harness_name="claude",
        deadline=server.time.monotonic() + 30,
    )

    assert keys_sent == [{"kind": "key", "value": "C-m"}]
    row = dashboard_db.get_session("auto-life")
    assert '"confirming_trust_prompt": false' in row["harness_state"]
