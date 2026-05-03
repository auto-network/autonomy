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
    CapabilityToolTarget,
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


def test_codex_interactive_resume_uses_resume_uuid(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    out = _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        resume_uuid="rollout-2026-05-02T20-00-00-12345678-1234-1234-1234-123456789abc",
    )
    assert out == "fake-container-id"
    cmd = captured_run[0]
    assert "resume" in cmd
    assert "12345678-1234-1234-1234-123456789abc" in cmd


def test_codex_noninteractive_uses_exec(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        image="autonomy-agent:dashboard",
        prompt="Write a summary.",
        model=None,
    )
    cmd = captured_run[0]
    assert "--entrypoint" in cmd
    assert "sh" in cmd
    shell_cmd = cmd[-1]
    assert "cat /workspace/output/.prompt.md | codex exec" in shell_cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in shell_cmd
    assert "--model" not in shell_cmd


def test_codex_noninteractive_does_not_require_claude_credentials(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    out = _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        image="autonomy-agent:dashboard",
        prompt="Open the workspace and inspect files.",
        model=None,
    )
    assert out == "fake-container-id"
    shell_cmd = captured_run[0][-1]
    assert "codex exec" in shell_cmd


def test_codex_noninteractive_resume_uses_exec_resume(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        prompt="Continue the run and write the result.",
        resume_uuid="rollout-2026-05-02T20-00-00-12345678-1234-1234-1234-123456789abc",
        model="gpt-5.4",
    )
    shell_cmd = captured_run[0][-1]
    assert "codex exec resume" in shell_cmd
    assert "12345678-1234-1234-1234-123456789abc" in shell_cmd
    assert "--model gpt-5.4" in shell_cmd


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


# ── tool_target / command-surface (auto-1webn.2) ────────────


def _jira_with_tool_target() -> MaterializedCapability:
    """Jira capability with the explicit `/opt/jira-tools` runtime model."""
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
        primer_path="agents/capabilities/jira/primer.md",
        skill_path="agents/capabilities/jira/SKILL.md",
        tool_target=CapabilityToolTarget(
            source="agents/capabilities/jira/tools",
            target="/opt/jira-tools",
            expose_commands=(
                "jira-read",
                "jira-comment",
                "jira-create",
                "jira-createmeta",
            ),
        ),
        env_bindings={
            "JIRA_EMAIL": "ops@example.com",
            "JIRA_BASE_URL": "https://example.atlassian.net",
        },
        secret_file_bindings={
            "/run/secrets/jira_token": "/etc/autonomy/secrets/jira_token",
        },
    )


def test_tool_target_mounts_source_at_absolute_target(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """Capability declares /opt/jira-tools — package mount must land there."""
    _run(
        output_dir=str(tmp_path / "run"),
        capabilities=(_jira_with_tool_target(),),
    )
    cmd = captured_run[0]
    mounts = _mounts(cmd)
    # The repo-local source must mount at the declared absolute target.
    assert any(
        m.endswith("agents/capabilities/jira/tools:/opt/jira-tools:ro")
        for m in mounts
    ), f"expected /opt/jira-tools mount, got {mounts}"
    # The package root mount is still there (capability inspectability).
    assert any(
        m.endswith(f"agents/capabilities/jira:{CAPABILITIES_MOUNT_DIR}/autonomy-jira:ro")
        for m in mounts
    )


def test_tool_target_creates_command_shims_with_correct_targets(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """Each expose_commands entry must produce an executable shim that
    invokes the declared tool target — making `jira-read` etc. resolvable
    through the mounted shim directory."""
    run_dir = tmp_path / "run"
    _run(
        output_dir=str(run_dir),
        capabilities=(_jira_with_tool_target(),),
    )
    shim_dir = run_dir / "cap-bin"
    assert shim_dir.is_dir(), f"shim directory not created at {shim_dir}"
    for cmd_name in ("jira-read", "jira-comment", "jira-create", "jira-createmeta"):
        shim = shim_dir / cmd_name
        assert shim.is_file(), f"shim missing for {cmd_name} at {shim}"
        # Executable bit must be set so the kernel can load the shim.
        import os, stat
        assert shim.stat().st_mode & stat.S_IXUSR, (
            f"shim {shim} is not executable"
        )
        body = shim.read_text()
        assert "/opt/jira-tools/" + cmd_name in body, (
            f"shim {shim} does not exec /opt/jira-tools/{cmd_name}: {body!r}"
        )


def test_tool_target_mounts_shim_dir_at_capability_bin(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """The shim directory must be mounted at /etc/autonomy/cap-bin so the
    image can prepend it to PATH for `jira-*` commands."""
    run_dir = tmp_path / "run"
    _run(
        output_dir=str(run_dir),
        capabilities=(_jira_with_tool_target(),),
    )
    cmd = captured_run[0]
    mounts = _mounts(cmd)
    shim_dir = run_dir / "cap-bin"
    assert f"{shim_dir}:/etc/autonomy/cap-bin:ro" in mounts


def test_tool_target_sets_capability_bin_env(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """Expose AUTONOMY_CAPABILITY_BIN so the container knows where shims live."""
    _run(
        output_dir=str(tmp_path / "run"),
        capabilities=(_jira_with_tool_target(),),
    )
    cmd = captured_run[0]
    envs = _envs(cmd)
    assert "AUTONOMY_CAPABILITY_BIN=/etc/autonomy/cap-bin" in envs


def test_tool_target_without_expose_commands_skips_shim_dir(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """A capability with a tool_target but no expose_commands still mounts
    the bundle but does not synthesize a shim directory."""
    cap = MaterializedCapability(
        contract="issue_tracker",
        contract_version=1,
        implementation="autonomy/jira",
        implementation_version=1,
        delivery_mode="mounted_tools",
        package_root="agents/capabilities/jira",
        mount_target=f"{CAPABILITIES_MOUNT_DIR}/autonomy-jira",
        tool_target=CapabilityToolTarget(
            source="agents/capabilities/jira/tools",
            target="/opt/jira-tools",
            expose_commands=(),
        ),
    )
    run_dir = tmp_path / "run"
    _run(output_dir=str(run_dir), capabilities=(cap,))
    cmd = captured_run[0]
    mounts = _mounts(cmd)
    envs = _envs(cmd)
    # tool_target source still mounts at /opt/jira-tools.
    assert any(m.endswith("agents/capabilities/jira/tools:/opt/jira-tools:ro") for m in mounts)
    # No shim directory mount or env when nothing to expose.
    assert not any(":/etc/autonomy/cap-bin:" in m for m in mounts)
    assert not any(e.startswith("AUTONOMY_CAPABILITY_BIN=") for e in envs)
    # And no cap-bin scaffold gets dropped on disk.
    assert not (run_dir / "cap-bin").exists()


# ── env_bindings source resolver ────────────────────────────────────────


class TestResolveEnvSource:
    """``env_bindings`` values support source schemes so the actual
    secret never sits in the graph DB. ``host:VAR_NAME`` forwards a
    dashboard-host env var; ``file:/path:VAR_NAME`` reads VAR_NAME from
    a dotenv-style file. Plain strings still pass through verbatim
    for backward compat with test fixtures and direct-literal callers.
    """

    def test_host_scheme_resolves_from_environ(self, monkeypatch):
        monkeypatch.setenv("DEMO_TOKEN", "ghp_resolved")
        assert session_launcher._resolve_env_source(
            "GH_TOKEN", "host:DEMO_TOKEN",
        ) == "ghp_resolved"

    def test_host_scheme_drops_when_unset(self, monkeypatch):
        monkeypatch.delenv("DEMO_MISSING", raising=False)
        assert session_launcher._resolve_env_source(
            "GH_TOKEN", "host:DEMO_MISSING",
        ) is None

    def test_host_scheme_rejects_empty_var_name(self):
        assert session_launcher._resolve_env_source("GH_TOKEN", "host:") is None

    def test_file_scheme_resolves_from_dotenv(self, tmp_path):
        envfile = tmp_path / "secrets.env"
        envfile.write_text(
            "# comment\n"
            "OTHER=ignored\n"
            'GH_TOKEN="ghp_from_file"\n'
            "BLANK=\n"
        )
        assert session_launcher._resolve_env_source(
            "GH_TOKEN", f"file:{envfile}:GH_TOKEN",
        ) == "ghp_from_file"

    def test_file_scheme_handles_unquoted_values(self, tmp_path):
        envfile = tmp_path / "secrets.env"
        envfile.write_text("GH_TOKEN=ghp_unquoted\n")
        assert session_launcher._resolve_env_source(
            "GH_TOKEN", f"file:{envfile}:GH_TOKEN",
        ) == "ghp_unquoted"

    def test_file_scheme_drops_when_file_missing(self, tmp_path):
        missing = tmp_path / "nope.env"
        assert session_launcher._resolve_env_source(
            "GH_TOKEN", f"file:{missing}:GH_TOKEN",
        ) is None

    def test_file_scheme_drops_when_var_absent(self, tmp_path):
        envfile = tmp_path / "secrets.env"
        envfile.write_text("OTHER=value\n")
        assert session_launcher._resolve_env_source(
            "GH_TOKEN", f"file:{envfile}:GH_TOKEN",
        ) is None

    def test_file_scheme_rejects_malformed_source(self):
        assert session_launcher._resolve_env_source(
            "GH_TOKEN", "file:/no/var/separator",
        ) is None

    def test_plain_literal_passes_through_for_backward_compat(self):
        # Existing test fixtures (e.g. _github_capability with
        # GH_TOKEN: ghp_value) and any direct-literal callers stay
        # working.
        assert session_launcher._resolve_env_source(
            "GH_TOKEN", "ghp_literal_value",
        ) == "ghp_literal_value"


def test_capability_env_drops_unresolved_host_binding_softly(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch,
):
    """An ``env_bindings`` value of ``host:GH_TOKEN`` with that env
    unset on the dashboard host must NOT crash launch — the binding
    drops silently and the capability's runtime probe later surfaces
    the ``env_missing`` reason for the operator."""
    cap = MaterializedCapability(
        contract="source_control",
        contract_version=1,
        implementation="autonomy/github",
        implementation_version=1,
        delivery_mode="image_baked",
        package_root="agents/capabilities/github",
        mount_target="/opt/autonomy/capabilities/autonomy-github",
        env_bindings={"GH_TOKEN": "host:DEMO_NOT_SET"},
    )
    monkeypatch.delenv("DEMO_NOT_SET", raising=False)

    _run(output_dir=str(tmp_path / "run"), capabilities=(cap,))
    cmd = captured_run[0]
    envs = _envs(cmd)
    # No GH_TOKEN env spec — binding silently dropped.
    assert not any(e.startswith("GH_TOKEN=") for e in envs)


def test_capability_env_resolves_host_binding_from_environ(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setenv("DEMO_GH", "ghp_from_host_env")
    cap = MaterializedCapability(
        contract="source_control",
        contract_version=1,
        implementation="autonomy/github",
        implementation_version=1,
        delivery_mode="image_baked",
        package_root="agents/capabilities/github",
        mount_target="/opt/autonomy/capabilities/autonomy-github",
        env_bindings={"GH_TOKEN": "host:DEMO_GH"},
    )

    _run(output_dir=str(tmp_path / "run"), capabilities=(cap,))
    cmd = captured_run[0]
    envs = _envs(cmd)
    assert "GH_TOKEN=ghp_from_host_env" in envs


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
