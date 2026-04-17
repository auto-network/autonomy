"""Tests for launch_session docker-run argv assembly.

Exercises the new ``privileged`` and ``startup_script`` params plus the
graph_project / graph_tags metadata → env passthrough. We capture the
docker argv by stubbing subprocess.run and asserting on the call list —
the container is never actually started.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents import session_launcher


@pytest.fixture
def fake_creds(monkeypatch):
    """Pretend an OAuth token was found in the env."""
    monkeypatch.setattr(
        session_launcher,
        "_resolve_credentials",
        lambda: {"type": "token", "token": "tok-xyz"},
    )


@pytest.fixture
def fake_crosstalk(monkeypatch):
    """Stub CrossTalk token insertion so tests don't hit auth_db."""
    import types
    fake = types.SimpleNamespace(insert_token=lambda *a, **kw: None)
    fake_dao = types.SimpleNamespace(auth_db=fake)
    monkeypatch.setitem(__import__("sys").modules, "tools.dashboard.dao", fake_dao)


@pytest.fixture
def captured_run(monkeypatch):
    calls: list[list[str]] = []

    class FakeCompleted:
        def __init__(self):
            self.returncode = 0
            self.stdout = "fake-container-id\n"
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted()

    monkeypatch.setattr(session_launcher.subprocess, "run", fake_run)
    return calls


def _run(**kw):
    """Call launch_session with common defaults filled in."""
    defaults = dict(
        session_type="dispatch",
        name="test-session",
        prompt=None,
        detach=True,
        image="autonomy-agent:enterprise",
    )
    defaults.update(kw)
    return session_launcher.launch_session(**defaults)


# ── privileged flag ──────────────────────────────────────────────────

def test_privileged_inserts_flag(tmp_path, fake_creds, fake_crosstalk, captured_run):
    _run(privileged=True, output_dir=str(tmp_path / "run"))
    assert len(captured_run) == 1
    cmd = captured_run[0]
    assert "--privileged" in cmd


def test_default_is_not_privileged(tmp_path, fake_creds, fake_crosstalk, captured_run):
    _run(output_dir=str(tmp_path / "run"))
    cmd = captured_run[0]
    assert "--privileged" not in cmd


# ── startup_script mount ─────────────────────────────────────────────

def test_startup_script_mounted_read_only(tmp_path, fake_creds, fake_crosstalk, captured_run):
    script = tmp_path / "startup.sh"
    script.write_text("#!/bin/bash\necho hi\n")
    _run(startup_script=str(script), output_dir=str(tmp_path / "run"))
    cmd = captured_run[0]
    # Find the -v entry that mounts our script.
    mount_spec = f"{script}:/startup.sh:ro"
    assert mount_spec in cmd


def test_startup_script_omitted_when_none(tmp_path, fake_creds, fake_crosstalk, captured_run):
    _run(output_dir=str(tmp_path / "run"))
    cmd = captured_run[0]
    assert not any(s.endswith(":/startup.sh:ro") for s in cmd)


# ── graph_project + graph_tags passthrough ───────────────────────────

def test_metadata_graph_project_exported(tmp_path, fake_creds, fake_crosstalk, captured_run):
    run_dir = tmp_path / "run"
    _run(
        output_dir=str(run_dir),
        metadata={"graph_project": "anchore", "graph_tags": ["enterprise", "ng"]},
    )
    cmd = captured_run[0]
    assert "GRAPH_SCOPE=anchore" in cmd
    assert "GRAPH_TAGS=enterprise,ng" in cmd

    # Meta doc on disk also carries them.
    meta = json.loads((run_dir / "sessions" / ".session_meta.json").read_text())
    assert meta["graph_project"] == "anchore"
    assert meta["graph_tags"] == ["enterprise", "ng"]


def test_graph_tags_string_passed_through_unchanged(tmp_path, fake_creds, fake_crosstalk, captured_run):
    _run(
        output_dir=str(tmp_path / "run"),
        metadata={"graph_project": "autonomy", "graph_tags": "dashboard"},
    )
    cmd = captured_run[0]
    assert "GRAPH_TAGS=dashboard" in cmd


def test_no_graph_env_without_metadata(tmp_path, fake_creds, fake_crosstalk, captured_run):
    _run(output_dir=str(tmp_path / "run"))
    cmd = captured_run[0]
    assert not any(s.startswith("GRAPH_SCOPE=") for s in cmd)
    assert not any(s.startswith("GRAPH_TAGS=") for s in cmd)


# ── Hardcoded license overlay removed (replaced by artifacts mechanism) ──────

def test_no_hardcoded_license_mount(tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch):
    """The ad-hoc /home/jeremy/workspace/license.yaml overlay must be gone.

    Artifact mounting is now driven by the ProjectConfig.artifacts layer and
    lands at /etc/autonomy/artifacts/ inside the container. This test pretends
    the old host license path exists and verifies launch_session does NOT
    inject a license.yaml mount by itself.
    """
    monkeypatch.setattr(session_launcher.Path, "exists", lambda self: True)
    _run(output_dir=str(tmp_path / "run"))
    cmd = captured_run[0]
    # No mount spec should embed an enterprise license overlay at the
    # workspace repo root or /etc/autonomy/artifacts — launch_session must
    # be agnostic to the artifact layer; callers inject mounts explicitly.
    assert not any("license.yaml" in s for s in cmd)
