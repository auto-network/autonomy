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
from agents.workspace_settings import (
    CAPABILITIES_MOUNT_DIR,
    MaterializedCapability,
)


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


# ── Codex interactive harness ───────────────────────────────────────

def test_codex_interactive_uses_codex_entrypoint(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        image="autonomy-agent:dashboard",
    )
    cmd = captured_run[0]
    assert "--entrypoint" in cmd
    assert "codex" in cmd
    assert "--no-alt-screen" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_codex_interactive_does_not_require_claude_credentials(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    out = _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        image="autonomy-agent:dashboard",
    )
    assert out == "fake-container-id"
    cmd = captured_run[0]
    assert "codex" in cmd


def test_codex_resume_not_supported_yet(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    out = _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        resume_uuid="session-123",
    )
    assert out is None
    assert captured_run == []


# ── Hardcoded license overlay removed (replaced by artifacts mechanism) ──────

def _github_capability() -> MaterializedCapability:
    return MaterializedCapability(
        contract="source_control",
        contract_version=1,
        implementation="autonomy/github",
        implementation_version=1,
        delivery_mode="image_baked",
        package_root="agents/capabilities/github",
        mount_target=f"{CAPABILITIES_MOUNT_DIR}/autonomy-github",
        required_env=("GH_TOKEN",),
        primer_path="agents/capabilities/github/primer.md",
        skill_path="agents/capabilities/github/SKILL.md",
        env_bindings={"GH_TOKEN": "ghp_value"},
    )


def _jira_capability() -> MaterializedCapability:
    return MaterializedCapability(
        contract="issue_tracker",
        contract_version=1,
        implementation="autonomy/jira",
        implementation_version=1,
        delivery_mode="mounted_tools",
        package_root="agents/capabilities/jira",
        mount_target=f"{CAPABILITIES_MOUNT_DIR}/autonomy-jira",
        required_env=("JIRA_EMAIL", "JIRA_BASE_URL"),
        required_secret_files=("/run/secrets/jira_token",),
        tool_paths=("agents/capabilities/jira/tools",),
        primer_path="agents/capabilities/jira/primer.md",
        skill_path="agents/capabilities/jira/SKILL.md",
        env_bindings={
            "JIRA_EMAIL": "ops@example.com",
            "JIRA_BASE_URL": "https://example.atlassian.net",
        },
        secret_file_bindings={
            "/run/secrets/jira_token": "/etc/autonomy/secrets/jira_token",
        },
    )


def _mounts(cmd: list[str]) -> list[str]:
    """Extract every ``-v`` mount spec value from a docker argv."""
    out = []
    for i, tok in enumerate(cmd):
        if tok == "-v" and i + 1 < len(cmd):
            out.append(cmd[i + 1])
    return out


def _envs(cmd: list[str]) -> list[str]:
    """Extract every ``-e`` env spec value from a docker argv."""
    out = []
    for i, tok in enumerate(cmd):
        if tok == "-e" and i + 1 < len(cmd):
            out.append(cmd[i + 1])
    return out


# ── Capability materialization (auto-uqq0i) ─────────────────────────


def test_no_capabilities_no_capability_mounts_or_env(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """No enabled capabilities → no /opt/autonomy/capabilities mounts or env."""
    _run(output_dir=str(tmp_path / "run"))
    cmd = captured_run[0]
    mounts = _mounts(cmd)
    envs = _envs(cmd)
    assert not any(CAPABILITIES_MOUNT_DIR in m for m in mounts)
    assert not any(e.startswith("GH_TOKEN=") for e in envs)
    assert not any(e.startswith("JIRA_") for e in envs)


def test_image_baked_capability_mounts_package_root_and_env(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(
        output_dir=str(tmp_path / "run"),
        capabilities=(_github_capability(),),
    )
    cmd = captured_run[0]
    mounts = _mounts(cmd)
    envs = _envs(cmd)

    # Package root is visible at the deterministic path.
    assert any(
        m.endswith(f"agents/capabilities/github:{CAPABILITIES_MOUNT_DIR}/autonomy-github:ro")
        for m in mounts
    )
    # GH_TOKEN env is bound from the org install.
    assert "GH_TOKEN=ghp_value" in envs
    # No Jira anything.
    assert not any("autonomy-jira" in m for m in mounts)
    assert not any("jira_token" in m for m in mounts)
    assert not any(e.startswith("JIRA_") for e in envs)


def test_mounted_tools_capability_secret_file_and_env(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(
        output_dir=str(tmp_path / "run"),
        capabilities=(_jira_capability(),),
    )
    cmd = captured_run[0]
    mounts = _mounts(cmd)
    envs = _envs(cmd)

    # Package root mount.
    assert any(
        m.endswith(f"agents/capabilities/jira:{CAPABILITIES_MOUNT_DIR}/autonomy-jira:ro")
        for m in mounts
    )
    # Secret-file mount lands at the declared container path.
    assert (
        "/etc/autonomy/secrets/jira_token:/run/secrets/jira_token:ro" in mounts
    )
    # Non-secret env bindings are exported.
    assert "JIRA_EMAIL=ops@example.com" in envs
    assert "JIRA_BASE_URL=https://example.atlassian.net" in envs


def test_secret_token_is_not_injected_as_env(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """Acceptance criterion: secret-file delivery stays file-based."""
    _run(
        output_dir=str(tmp_path / "run"),
        capabilities=(_jira_capability(),),
    )
    cmd = captured_run[0]
    envs = _envs(cmd)
    # The Jira token must NOT appear as an env var (no JIRA_TOKEN= etc.).
    for env_spec in envs:
        assert "jira_token" not in env_spec.lower()
        assert "JIRA_TOKEN" not in env_spec
        # Sanity: the secret host path must not have leaked into env either.
        assert "/etc/autonomy/secrets/jira_token" not in env_spec


def test_both_capabilities_render_without_clobber(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(
        output_dir=str(tmp_path / "run"),
        capabilities=(_github_capability(), _jira_capability()),
    )
    cmd = captured_run[0]
    mounts = _mounts(cmd)
    envs = _envs(cmd)

    # Both package roots present.
    assert any("autonomy-github" in m for m in mounts)
    assert any("autonomy-jira" in m for m in mounts)
    # All three env vars present.
    assert "GH_TOKEN=ghp_value" in envs
    assert "JIRA_EMAIL=ops@example.com" in envs
    assert "JIRA_BASE_URL=https://example.atlassian.net" in envs
    # Secret file present.
    assert (
        "/etc/autonomy/secrets/jira_token:/run/secrets/jira_token:ro" in mounts
    )


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
