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
    assert row["state"] == "ACTIVE"
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
    assert row["state"] == "FAILED"
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
    assert row["state"] == "FAILED"
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


def test_wait_for_prompt_waits_for_poller_signal(monkeypatch, tmp_path):
    """_wait_for_prompt is a signal-waiter: the pane-poller is the single
    pane reader/keystroke sender; the worker step waits on the durable
    harness_state.composer_ready flag it persists."""
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="auto-life",
        session_type="container",
        project="blindhash-operations",
        harness="claude",
    )

    sleeps = []

    def _sleep_then_signal(seconds):
        sleeps.append(seconds)
        # Simulate the pane-poller confirming the composer on the second
        # worker poll.
        if len(sleeps) == 2:
            dashboard_db.update_tail_state(
                "auto-life", harness_state='{"composer_ready": true}',
            )

    monkeypatch.setattr(server.time, "sleep", _sleep_then_signal)

    server._wait_for_prompt(
        tmux_name="auto-life",
        deadline=server.time.monotonic() + 30,
    )

    assert len(sleeps) >= 2  # waited, did not return before the signal


def test_wait_for_prompt_times_out_without_signal(monkeypatch, tmp_path):
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="auto-life",
        session_type="container",
        project="blindhash-operations",
        harness="claude",
    )

    with pytest.raises(TimeoutError):
        server._wait_for_prompt(
            tmux_name="auto-life",
            deadline=server.time.monotonic(),  # already expired
        )


def test_resume_start_handler_host_kind_runs_to_running(monkeypatch, tmp_path):
    """Host resume through the worker: host_cmd prebuilt by the API handler,
    tmux spawned with -c REPO_ROOT, composer signal waited, resume message
    injected, row lands running."""
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="host-life",
        session_type="host",
        project="host-proj",
        harness="claude",
    )
    calls = {"tmux": [], "inject": []}

    def fake_subprocess_run(cmd, **kwargs):
        calls["tmux"].append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(server, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(server.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(server, "_wait_for_prompt", lambda **_kwargs: None)
    monkeypatch.setattr(
        server, "_render_resume_message", lambda **_kwargs: "resumed orientation",
    )
    monkeypatch.setattr(
        server, "_inject_echo_verified",
        lambda **kwargs: calls["inject"].append(kwargs),
    )

    server._run_session_resume_start(
        LifecycleJob("start", "host-life", {
            "resume": True,
            "kind": "host",
            "host_cmd": "claude --resume abc",
            "jsonl_path": str(tmp_path / "x.jsonl"),
            "resume_uuid": "abc",
            "harness": "claude",
            "revived": True,
        }),
        SessionLifecycleStateWriter(),
    )

    row = dashboard_db.get_session("host-life")
    assert row["startup_state"] is None
    assert row["state"] == "ACTIVE"
    spawn_cmd = calls["tmux"][0][0]
    assert spawn_cmd[:2] == ["tmux", "new-session"]
    assert "-c" in spawn_cmd and str(tmp_path) in spawn_cmd
    assert spawn_cmd[-1] == "claude --resume abc"
    assert calls["inject"] and calls["inject"][0]["message"] == "resumed orientation"


def test_resume_start_handler_failure_preserves_worktrees(monkeypatch, tmp_path):
    """A failed RESUME must never delete the session's worktrees — they
    carry the session's whole uncommitted/unmerged history."""
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="auto-life",
        session_type="container",
        project="blindhash-operations",
        harness="claude",
    )
    proj = _project()
    wt_cleanups = []

    monkeypatch.setattr(server, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _pid: proj)
    monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _proj: {})
    monkeypatch.setattr(server, "render_workspace_primer", lambda _proj: "primer")
    monkeypatch.setattr(
        server, "prepare_session_mounts", lambda *_a, **_kw: {},
    )
    monkeypatch.setattr(server, "launch_session", lambda **_kw: "echo launched")
    monkeypatch.setattr(
        server, "cleanup_session_worktrees",
        lambda *_a, **_kw: wt_cleanups.append(True),
    )

    def failing_tmux(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stderr=b"tmux exploded")

    monkeypatch.setattr(server.subprocess, "run", failing_tmux)

    server._run_session_resume_start(
        LifecycleJob("start", "auto-life", {
            "resume": True,
            "kind": "project",
            "project_id": "blindhash-operations",
            "output_dir": str(tmp_path / "run"),
            "jsonl_path": str(tmp_path / "x.jsonl"),
            "resume_uuid": "abc",
            "harness": "claude",
            "revived": True,
        }),
        SessionLifecycleStateWriter(),
    )

    row = dashboard_db.get_session("auto-life")
    assert row["state"] == "FAILED"
    assert "tmux creation failed" in row["lifecycle_detail"]
    assert wt_cleanups == []  # worktrees untouched


def test_stop_handler_reaches_dead_despite_step_errors(monkeypatch, tmp_path):
    """STOP runs stopping -> cleaning -> dead with bounded, idempotent
    steps; a failing step degrades to a warning and the session still
    reaches dead — a half-stopped session must not stay running."""
    from tools.dashboard import server

    _init_db(tmp_path)
    dashboard_db.insert_session(
        tmux_name="auto-life",
        session_type="container",
        project="blindhash-operations",
        harness="claude",
    )

    def exploding_kill(cmd, **kwargs):
        raise RuntimeError("docker unreachable")

    monkeypatch.setattr(server.subprocess, "run", exploding_kill)
    monkeypatch.setattr(server.auth_db, "revoke_token", lambda _name: None)

    server._run_session_stop(
        LifecycleJob("stop", "auto-life", {}),
        SessionLifecycleStateWriter(),
    )

    row = dashboard_db.get_session("auto-life")
    assert row["state"] == "ENDED"
    assert row["startup_state"] is None


def test_verify_container_started_fails_fast_with_pane_tail(monkeypatch):
    """docker run producing NO container must fail the launch within the
    verification window — not sit in phantom setup for the full 600s
    budget (auto-0709-092918: an OCI mount error printed in the pane in
    2s; the launch waited 600s, three times)."""
    from tools.dashboard import server

    monkeypatch.setattr(server, "_container_exists", lambda _n: False)
    monkeypatch.setattr(
        server, "_run_tmux_capture",
        lambda _n, **_kw: "docker: OCI runtime create failed: ro mkdir\n",
    )
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError) as exc:
        server._verify_container_started(
            tmux_name="auto-life",
            deadline=server.time.monotonic() + 0.2,  # a couple of probes
        )
    assert "no container" in str(exc.value)
    assert "OCI runtime create failed" in str(exc.value)


def test_verify_container_probe_failure_is_not_a_verdict(monkeypatch):
    """A failing docker probe (fork pressure) must not claim 'no
    container' — same fail-safe contract as the tmux liveness probe."""
    from tools.dashboard import server

    monkeypatch.setattr(server, "_container_exists", lambda _n: None)
    monkeypatch.setattr(server, "_run_tmux_capture", lambda _n, **_kw: "")
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError) as exc:
        server._verify_container_started(
            tmux_name="auto-life",
            deadline=server.time.monotonic(),
        )
    assert "could not be verified" in str(exc.value)


def test_wait_for_setup_fails_when_container_dies_mid_setup(monkeypatch, tmp_path):
    """A container that dies during setup can never write .setup-exit —
    two consecutive authoritative 'gone' probes fail the step immediately
    instead of waiting out the 600s budget."""
    from tools.dashboard import server

    monkeypatch.setattr(server, "_container_exists", lambda _n: False)
    monkeypatch.setattr(
        server, "_run_tmux_capture", lambda _n, **_kw: "container exited\n",
    )
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError) as exc:
        server._wait_for_setup_complete(
            tmux_name="auto-life",
            run_dir=tmp_path,
            startup_script=tmp_path / "startup.sh",
            deadline=server.time.monotonic() + 60,
        )
    assert "died during setup" in str(exc.value)
