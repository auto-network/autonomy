"""Regression tests for the foreground launch entry point (auto-vm8qh).

vm8qh unifies the detached (launch_session) and foreground (launch_session_cli)
consumers on ONE declare->resolve->emit mount path, so a credential-ordering
guard proven on one entry point must hold on the other too."""
import subprocess
import sys
import types

import pytest

from agents import launch_session_cli, session_launcher


@pytest.fixture(autouse=True)
def signin_deliveries(monkeypatch):
    """Record background sign-in deliveries instead of starting the thread
    that waits for a real container."""
    got = []
    monkeypatch.setattr(session_launcher, "deliver_signins_in_background",
                        lambda name, payloads: got.append((name, sorted(payloads))))
    return got


def _foreground_argv(tmp_path, monkeypatch, harness="codex"):
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    monkeypatch.setattr(launch_session_cli, "_workspace_model", lambda wid: None)
    monkeypatch.setattr(launch_session_cli, "_workspace_runtime",
                        lambda wid: (False, "standard"))
    monkeypatch.setattr(sys, "argv", [
        "launch_session_cli", "--harness", harness, "--name", "t",
        "--org", "o", "--image", "x", "--output-dir", str(tmp_path / "run"),
    ])


def test_foreground_unopenable_signin_refuses_before_docker(tmp_path, monkeypatch):
    """A vault sign-in chosen for the launch that cannot be opened is a
    refusal on the foreground path too: main() returns 1 and Docker never
    runs (auto-vm8qh criterion 6, auto-1cc4q)."""
    _foreground_argv(tmp_path, monkeypatch)
    monkeypatch.setattr(session_launcher, "_signin_payloads", lambda acct: None)
    ran = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: ran.append(a) or types.SimpleNamespace(returncode=0))
    assert launch_session_cli.main() == 1
    assert ran == [], "Docker must not run on a refused foreground launch"


def test_foreground_delivers_signins_into_the_ramfs(tmp_path, monkeypatch,
                                                     signin_deliveries):
    """auto-1cc4q: the foreground container gets its sign-ins delivered, not
    mounted, and its harness argv waits for and links them."""
    _foreground_argv(tmp_path, monkeypatch)
    monkeypatch.setattr(session_launcher, "_signin_payloads",
                        lambda acct: {session_launcher.CODEX_AUTH_FILENAME: b"{}"})
    captured = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: captured.update(cmd=cmd)
                        or types.SimpleNamespace(returncode=0))
    assert launch_session_cli.main() == 0
    cmd = captured["cmd"]
    assert signin_deliveries == [("t", [session_launcher.CODEX_AUTH_FILENAME])]
    assert "/home/agent/.codex/auth.json:ro" not in " ".join(cmd)
    i = cmd.index("x")
    assert cmd[i + 1:i + 3] == ["sh", "-c"] and cmd[i + 4] == "autonomy-signin"
    assert cmd[i + 5] == "codex"
    assert not list((tmp_path / "run").rglob("codex-auth.json"))


# ── network_host forwarding (auto-0l7jg) ──────────────────────────────────────
# The per-workspace network_host privilege was honored only by dashboard
# launches; the bead-dispatch path ignored it, so a workspace flipped to
# network_host:false still came up host-networked and could bind host ports.

def test_workspace_network_host_resolves_from_workspace(monkeypatch):
    """A resolved workspace's network_host flows through; an empty/unknown
    workspace-id falls back to True (launch_session's own default), so an
    unmatched bead's networking is unchanged."""
    ws = types.SimpleNamespace(network_host=False)
    monkeypatch.setattr(launch_session_cli, "load_workspaces", lambda: {"autonomy": ws})

    assert launch_session_cli._workspace_network_host("autonomy") is False
    assert launch_session_cli._workspace_network_host("unknown-ws") is True
    assert launch_session_cli._workspace_network_host("") is True


def test_detach_forwards_workspace_network_host(tmp_path, monkeypatch):
    """The dispatch (detached) path forwards the workspace's network_host into
    launch_session instead of taking the dataclass default True."""
    monkeypatch.setattr(launch_session_cli, "_workspace_model", lambda wid: None)
    monkeypatch.setattr(launch_session_cli, "_workspace_runtime",
                        lambda wid: (False, "standard"))
    monkeypatch.setattr(launch_session_cli, "_workspace_network_host",
                        lambda wid: False)

    captured = {}

    def fake_launch_session(**kwargs):
        captured.update(kwargs)
        return "container-abc"

    monkeypatch.setattr(launch_session_cli, "launch_session", fake_launch_session)

    monkeypatch.setattr(sys, "argv", [
        "launch_session_cli", "--name", "t", "--org", "o", "--image", "x",
        "--workspace-id", "autonomy", "--output-dir", str(tmp_path / "run"),
        "--detach",
    ])
    rc = launch_session_cli.main()

    assert rc == 0
    assert captured["network_host"] is False, \
        "detached dispatch must forward the workspace network_host, not default True"


def _run_foreground_capture(tmp_path, monkeypatch, *, network_host, topo):
    """Drive the foreground launch far enough to capture the docker argv,
    stubbing credentials, the platform snapshot, and topology."""
    monkeypatch.setattr(launch_session_cli, "_workspace_model", lambda wid: None)
    monkeypatch.setattr(launch_session_cli, "_workspace_runtime",
                        lambda wid: (False, "standard"))
    monkeypatch.setattr(launch_session_cli, "_workspace_network_host",
                        lambda wid: network_host)
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    monkeypatch.setattr(session_launcher, "_resolve_credentials",
                        lambda: {"type": "token", "token": "t"})
    monkeypatch.setattr(session_launcher, "_signin_payloads", lambda acct: {})
    monkeypatch.setattr(session_launcher, "_setup_auth_docker_args",
                        lambda creds, run_dir: [])

    import agents.mount_plan as mount_plan
    monkeypatch.setattr(mount_plan, "discover_topology", lambda: topo)

    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    monkeypatch.setattr(sys, "argv", [
        "launch_session_cli", "--harness", "claude", "--name", "t",
        "--org", "o", "--image", "x", "--workspace-id", "autonomy",
        "--output-dir", str(tmp_path / "run"),
    ])
    rc = launch_session_cli.main()
    assert rc == 0
    return captured["cmd"]


def test_foreground_host_networking_when_workspace_grants_it(tmp_path, monkeypatch):
    """network_host=True keeps the host-network topology + localhost GRAPH_API."""
    from agents.mount_plan import NodeTopology
    cmd = _run_foreground_capture(
        tmp_path, monkeypatch,
        network_host=True, topo=NodeTopology(is_host_process=True),
    )
    assert "--network=host" in cmd
    assert "GRAPH_API=https://localhost:8080" in cmd
    assert not any(v.startswith("BEADS_DOLT_SERVER_HOST=") for v in cmd)


def test_foreground_bridge_when_workspace_denies_host_net(tmp_path, monkeypatch):
    """network_host=False on a host-process node drops --network=host for the
    host.docker.internal bridge topology — no more binding host ports."""
    from agents.mount_plan import NodeTopology
    cmd = _run_foreground_capture(
        tmp_path, monkeypatch,
        network_host=False, topo=NodeTopology(is_host_process=True),
    )
    assert "--network=host" not in cmd
    assert "--add-host=host.docker.internal:host-gateway" in cmd
    assert "GRAPH_API=https://host.docker.internal:8080" in cmd
