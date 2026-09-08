"""Regression tests for the foreground launch entry point (auto-vm8qh).

vm8qh unifies the detached (launch_session) and foreground (launch_session_cli)
consumers on ONE declare->resolve->emit mount path, so a credential-ordering
guard proven on one entry point must hold on the other too."""
import subprocess
import sys
import types
from pathlib import Path

from agents import launch_session_cli, session_launcher


def _stub_usable_codex_row(monkeypatch):
    """Make a usable Codex credential row exist so build_mount_plan DECLARES the
    auth mount — without this the declare/materialize distinction has nothing to
    prove on the foreground path either."""
    monkeypatch.setattr(session_launcher, "_pick_codex_credential_row",
                        lambda rows: types.SimpleNamespace(key="acct", payload={}))
    monkeypatch.setattr(session_launcher, "_codex_credential_rows", lambda: [object()])


def test_foreground_declared_credential_failed_materialization_refuses_before_docker(
    tmp_path, monkeypatch,
):
    """A Codex credential DECLARED by build_mount_plan but that fails to
    materialize is a launch refusal on the foreground path: main() returns 1,
    subprocess.run is never called (Docker never runs), and no partial credential
    file remains. Otherwise the already-validated argv binds a path that was never
    written — host-process -v fabricates a dir there, the fallback bind fails only
    at docker-run — the exact late-failure the detached guard already closes."""
    _stub_usable_codex_row(monkeypatch)
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    monkeypatch.setattr(launch_session_cli, "_workspace_model", lambda wid: None)
    monkeypatch.setattr(launch_session_cli, "_workspace_runtime",
                        lambda wid: (False, "standard"))

    run_dir = tmp_path / "run"
    partial = run_dir / "codex-auth.json"

    def failing_materialize(rd):
        # A write that landed before failing (or a chmod failure): a file is on
        # disk, but the materializer reports failure by returning None.
        Path(rd).joinpath("codex-auth.json").write_text("partial-credential")
        return None

    monkeypatch.setattr(session_launcher, "_materialize_codex_auth_json",
                        failing_materialize)

    ran = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: ran.append(a) or types.SimpleNamespace(returncode=0))

    monkeypatch.setattr(sys, "argv", [
        "launch_session_cli", "--harness", "codex", "--name", "t",
        "--org", "o", "--image", "x", "--output-dir", str(run_dir),
    ])
    rc = launch_session_cli.main()

    assert rc == 1, "foreground must refuse when a declared credential fails to materialize"
    assert ran == [], "Docker must not run on a refused foreground launch"
    assert not partial.exists(), "the refusal path must leave no partial credential file behind"


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
                        lambda: {"creds_copy": None})
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
