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
