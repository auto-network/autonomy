"""Legacy host-tmux panes stay visible and drivable after a node moves to the
tmux sidecar; new sessions are created on the sidecar only (operator direction
2026-09-26, epic auto-e9mpm)."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from tools.dashboard import tmux_route
from tools.dashboard.dao import dashboard_db


@pytest.fixture
def servers(tmp_path, monkeypatch):
    """A sidecar default server and a host server, as socket files, with the
    dashboard DB and the route cache isolated."""
    sidecar_dir = tmp_path / "run-autonomy-tmux"
    host_dir = tmp_path / "host-tmp"
    uid = os.getuid()
    for d in (sidecar_dir, host_dir):
        (d / f"tmux-{uid}").mkdir(parents=True)
        (d / f"tmux-{uid}" / "default").write_text("")
    monkeypatch.setenv("TMUX_TMPDIR", str(sidecar_dir))
    monkeypatch.setattr(tmux_route, "LEGACY_HOST_TMPDIR", str(host_dir))
    monkeypatch.setattr(tmux_route, "_cache", {})
    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None
    dashboard_db.init_db(tmp_path / "dashboard.db")
    yield SimpleNamespace(
        sidecar=str(sidecar_dir / f"tmux-{uid}" / "default"),
        host=str(host_dir / f"tmux-{uid}" / "default"),
    )
    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None


def _row(name: str, socket: str | None = None) -> None:
    dashboard_db.insert_session(
        tmux_name=name, session_type="container", project="p", harness="claude",
    )
    if socket:
        dashboard_db.set_tmux_socket(name, socket)


def test_without_a_host_server_every_command_is_plain_tmux(tmp_path, monkeypatch):
    monkeypatch.setattr(tmux_route, "LEGACY_HOST_TMPDIR", str(tmp_path / "absent"))
    monkeypatch.setattr(tmux_route, "_cache", {})
    assert tmux_route.legacy_socket() is None
    assert tmux_route.argv("auto-1", "capture-pane", "-p", "-t", "auto-1") == [
        "tmux", "capture-pane", "-p", "-t", "auto-1",
    ]


def test_a_session_recorded_on_the_host_server_is_driven_there(servers):
    _row("auto-old", servers.host)
    assert tmux_route.argv("auto-old:0.0", "send-keys", "-t", "auto-old:0.0", "\r") == [
        "tmux", "-S", servers.host, "send-keys", "-t", "auto-old:0.0", "\r",
    ]


def test_a_session_created_now_is_recorded_on_the_sidecar(servers):
    _row("auto-new")
    tmux_route.record_created("auto-new")
    assert dashboard_db.get_tmux_socket("auto-new") == servers.sidecar
    assert tmux_route.argv("auto-new", "has-session", "-t", "auto-new") == [
        "tmux", "has-session", "-t", "auto-new",
    ]


def test_a_row_without_a_socket_is_probed_once_and_recorded(servers, monkeypatch):
    _row("auto-legacy")
    probes = []

    def fake_run(cmd, **kwargs):
        probes.append(cmd)
        on_host = cmd[:3] == ["tmux", "-S", servers.host]
        return SimpleNamespace(returncode=0 if on_host else 1)

    monkeypatch.setattr(tmux_route.subprocess, "run", fake_run)
    assert tmux_route.socket_for("auto-legacy") == servers.host
    assert dashboard_db.get_tmux_socket("auto-legacy") == servers.host
    count = len(probes)
    tmux_route.socket_for("auto-legacy")
    assert len(probes) == count, "resolved once, then cached"


def test_a_restart_moves_a_legacy_session_to_the_sidecar(servers):
    _row("auto-moved", servers.host)
    assert tmux_route.socket_for("auto-moved") == servers.host
    tmux_route.record_created("auto-moved")  # the relaunch's new-session
    assert tmux_route.socket_for("auto-moved") == servers.sidecar


def test_listing_merges_both_servers(servers, monkeypatch):
    def fake_run(cmd, **kwargs):
        out = "host-0916-103518\nauto-old\n" if "-S" in cmd else "auto-new\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(tmux_route.subprocess, "run", fake_run)
    result = tmux_route.list_sessions_result()
    assert result.returncode == 0
    assert result.stdout.split() == ["auto-new", "host-0916-103518", "auto-old"]


def test_listing_answers_when_only_the_host_server_is_up(servers, monkeypatch):
    def fake_run(cmd, **kwargs):
        if "-S" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "auto-old\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "no server running")

    monkeypatch.setattr(tmux_route.subprocess, "run", fake_run)
    result = tmux_route.list_sessions_result()
    assert result.returncode == 0 and result.stdout.split() == ["auto-old"]


def test_a_native_host_transcript_is_read_through_the_home_mount(tmp_path, monkeypatch):
    from tools.dashboard import session_monitor
    import tools.data_paths as data_paths

    mount = tmp_path / "host-home"
    transcript = mount / ".claude" / "projects" / "-opt-autonomy-code" / "u.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    monkeypatch.setattr(data_paths, "HOST_HOME_MOUNT", mount)
    monkeypatch.setenv("AUTONOMY_HOST_HOME", "/home/operator")
    stored = "/home/operator/.claude/projects/-opt-autonomy-code/u.jsonl"
    assert session_monitor._local_agent_runs_path(stored) == str(transcript)
    assert session_monitor._local_agent_runs_path("/elsewhere/u.jsonl") == "/elsewhere/u.jsonl"
