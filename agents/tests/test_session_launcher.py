"""Tests for launch_session docker-run argv assembly.

Exercises independent nested-Docker and isolation-runtime settings plus the
startup script and graph_project / graph_tags metadata → env passthrough. We capture the
docker argv by stubbing subprocess.run and asserting on the call list —
the container is never actually started.
"""

from __future__ import annotations

import json
import os
import subprocess
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
    """Capture the docker invocations launch_session makes.

    Only ``docker`` commands are recorded: every assertion in this file
    indexes the docker run/exec command directly, and the launch path also
    shells out to helpers (e.g. ``git rev-parse`` for Codex trust rooting,
    2277f887) that would otherwise shift the indices each time one is added.
    """
    calls: list[list[str]] = []

    class FakeCompleted:
        def __init__(self):
            self.returncode = 0
            self.stdout = "fake-container-id\n"
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["docker", "run"]:
            calls.append(cmd)
        return FakeCompleted()

    monkeypatch.setattr(session_launcher.subprocess, "run", fake_run)
    return calls


@pytest.fixture(autouse=True)
def platform_snapshot(monkeypatch, tmp_path):
    """Stub the platform-snapshot git preparation with a tmp directory.

    ``_ensure_platform_snapshot`` runs real git (clone/fetch/checkout of the
    live checkout) — tests must never do that. The stub records whether the
    launcher asked for a snapshot and what path it mounted.
    """
    snap = tmp_path / "platform-snapshot"
    # A real snapshot checkout materializes data/uploads via the tracked
    # .gitkeep — the uploads bind's mount point (auto-j3oj3 smoke failure).
    (snap / "data" / "uploads").mkdir(parents=True, exist_ok=True)
    calls: list[int] = []

    def fake() -> str:
        calls.append(1)
        return str(snap)

    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", fake)
    fake.calls = calls  # type: ignore[attr-defined]
    fake.path = snap  # type: ignore[attr-defined]
    return fake


@pytest.fixture(autouse=True)
def neutralize_launch_preflight(monkeypatch):
    """Every test here asserts on the ASSEMBLED docker argv with docker and the
    filesystem stubbed; none stages the real image, runtime, or mount sources.
    The launcher now preflights all three against the daemon and refuses a
    missing one by name — so on this machine (no such image built, no
    ``<repo>/.beads``) it would refuse every launch under test. Report 'nothing
    missing' by default; the dedicated preflight tests below re-patch this to
    stage an absence and assert the refusal."""
    from agents import launch_preflight
    monkeypatch.setattr(launch_preflight, "preflight", lambda **kw: [])


def _run(**kw):
    """Call launch_session with common defaults filled in.

    A container launch now fails closed without a canonical ``metadata["org"]``
    to stamp on its session token, so the default metadata carries one. Tests
    that pass their own ``metadata`` control it fully (that is how the
    fail-closed cases below omit the org deliberately)."""
    defaults = dict(
        session_type="dispatch",
        name="test-session",
        prompt=None,
        detach=True,
        image="session-enterprise",
        metadata={"org": "test-org"},
    )
    defaults.update(kw)
    return session_launcher.launch_session(**defaults)


# ── nested Docker and isolation runtime ──────────────────────────────

def test_nested_docker_defaults_to_privileged_runtime(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    run_dir = tmp_path / "run"
    _run(needs_nested_docker=True, output_dir=str(run_dir))
    assert len(captured_run) == 1
    cmd = captured_run[0]
    assert "--privileged" in cmd
    assert not any(arg.startswith("--runtime=") for arg in cmd)
    # DinD keeps its wrapper entrypoint and receives the full harness argv.
    image_index = cmd.index("session-enterprise")
    assert cmd[image_index + 1] == "claude"
    assert "--entrypoint" not in cmd
    assert "/var/run/docker.sock" not in " ".join(cmd)
    meta = json.loads((run_dir / "sessions" / ".session_meta.json").read_text())
    assert meta["needs_nested_docker"] is True
    assert meta["session_runtime"] == "privileged"


def test_standard_session_is_not_privileged(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(output_dir=str(tmp_path / "run"))
    cmd = captured_run[0]
    assert "--privileged" not in cmd
    assert not any(arg.startswith("--runtime=") for arg in cmd)
    assert "--entrypoint" not in cmd
    assert "/var/run/docker.sock" not in " ".join(cmd)


@pytest.mark.parametrize(
    ("runtime", "expected_arg"),
    [
        ("standard", None),
        ("sysbox", "--runtime=sysbox-runc"),
        ("runsc", "--runtime=runsc"),
    ],
)
def test_nested_docker_honors_configured_runtime_without_privileged(
    tmp_path, fake_creds, fake_crosstalk, captured_run, runtime, expected_arg,
):
    _run(
        needs_nested_docker=True,
        runtime=runtime,
        output_dir=str(tmp_path / "run"),
    )
    cmd = captured_run[0]
    if expected_arg is None:
        assert not any(arg.startswith("--runtime=") for arg in cmd)
    else:
        assert expected_arg in cmd
    assert "--privileged" not in cmd
    image_index = cmd.index("session-enterprise")
    assert cmd[image_index + 1] == "claude"
    assert "/var/run/docker.sock" not in " ".join(cmd)


def test_runtime_is_independent_of_nested_docker_entrypoint(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(
        needs_nested_docker=False,
        runtime="privileged",
        prompt="hello",
        output_dir=str(tmp_path / "run"),
    )
    cmd = captured_run[0]
    assert "--privileged" in cmd
    # Isolation alone must not make a base image behave like DinD.
    assert "--entrypoint" in cmd
    assert cmd[cmd.index("--entrypoint") + 1] == "sh"


@pytest.mark.parametrize(
    "mounts",
    [
        {"/var/run/docker.sock": "/tmp/nested.sock"},
        {"/tmp/not-a-socket": "/var/run/docker.sock"},
    ],
)
@pytest.mark.parametrize("runtime", ["standard", "privileged", "sysbox"])
def test_host_docker_socket_is_refused_under_every_runtime(
    tmp_path, fake_creds, fake_crosstalk, captured_run, mounts, runtime,
):
    result = _run(
        mounts=mounts,
        runtime=runtime,
        output_dir=str(tmp_path / f"run-{runtime}"),
    )
    assert result is None
    assert captured_run == []


def test_invalid_runtime_fails_before_docker(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    result = _run(
        runtime="sysbox;touch-pwned",
        output_dir=str(tmp_path / "run"),
    )
    assert result is None
    assert captured_run == []


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
        metadata={"org": "anchore", "graph_project": "anchore",
                  "graph_tags": ["enterprise", "ng"]},
    )
    cmd = captured_run[0]
    assert "GRAPH_TAGS=enterprise,ng" in cmd

    # Meta doc on disk also carries them.
    meta = json.loads((run_dir / "sessions" / ".session_meta.json").read_text())
    assert meta["graph_project"] == "anchore"
    assert meta["graph_tags"] == ["enterprise", "ng"]


def test_graph_tags_string_passed_through_unchanged(tmp_path, fake_creds, fake_crosstalk, captured_run):
    _run(
        output_dir=str(tmp_path / "run"),
        metadata={"org": "autonomy", "graph_tags": "dashboard"},
    )
    cmd = captured_run[0]
    assert "GRAPH_TAGS=dashboard" in cmd


def test_no_graph_tags_without_tags(tmp_path, fake_creds, fake_crosstalk, captured_run):
    # A launch with no tags exports no GRAPH_TAGS.
    _run(output_dir=str(tmp_path / "run"))
    cmd = captured_run[0]
    assert not any(s.startswith("GRAPH_TAGS=") for s in cmd)


def test_launch_without_canonical_org_fails_closed(tmp_path, fake_creds, fake_crosstalk, captured_run):
    # A container that cannot be assigned an org must not receive a token; the
    # launch fails and no docker command is issued. graph_project/graph_org are
    # deliberately NOT accepted as the token-stamp source.
    for md in ({}, {"graph_project": "anchore"}, {"graph_org": "anchore"}):
        captured_run.clear()
        result = _run(output_dir=str(tmp_path / "run"), metadata=md)
        assert result is None
        assert captured_run == []


# ── Codex interactive harness ───────────────────────────────────────

def test_codex_interactive_uses_codex_entrypoint(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        image="autonomy-session-platform",
    )
    cmd = captured_run[0]
    assert "--entrypoint" in cmd
    assert "codex" in cmd
    assert "--no-alt-screen" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_launch_refuses_and_names_missing_inputs(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch, capsys,
):
    """A missing launch input — image, runtime, or mount source — refuses the
    launch BY NAME before docker run, never the nameless 'produced no container'
    that cost an hour on the node. All problems are reported at once; docker is
    never invoked."""
    from agents import launch_preflight
    from agents.launch_preflight import LaunchProblem
    monkeypatch.setattr(launch_preflight, "preflight", lambda **kw: [
        LaunchProblem("image", "autonomy-session-platform", "not built — run `agents/build.sh`."),
        LaunchProblem("mount", "/x/.beads", "source does not exist (would mount at /data/.beads)."),
    ])
    out = _run(output_dir=str(tmp_path / "run"))
    assert out is None                       # refused, not launched
    assert captured_run == []                # docker run never invoked
    err = capsys.readouterr().err
    assert "2 launch input(s) missing" in err
    assert "[image] autonomy-session-platform" in err  # the image is named
    assert "[mount] /x/.beads" in err                 # and the mount, together
    assert "agents/build.sh" in err                   # with the fix


def test_workspace_env_credential_resolves_to_env_arg(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch,
):
    """A workspace env value of credential:<key> resolves through the vault into
    the container's -e; literals — including scheme-looking ones — pass through
    unchanged (workspace env is not blanket source-resolved)."""
    monkeypatch.setattr(session_launcher, "_resolve_credential", lambda key: "ghp_REAL")
    _run(output_dir=str(tmp_path / "run"), extra_env={
        "GH_TOKEN": "credential:github.token",
        "PLAIN": "literal-value",
        "HOSTY": "host:8080",   # literal — must NOT be reinterpreted as a scheme
    })
    joined = " ".join(captured_run[0])
    assert "GH_TOKEN=ghp_REAL" in joined            # resolved from the vault
    assert "PLAIN=literal-value" in joined          # literal passes through
    assert "HOSTY=host:8080" in joined              # scheme-looking literal untouched
    assert "credential:github.token" not in joined  # never the raw pointer


def test_workspace_env_credential_dropped_when_unavailable(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch,
):
    """A credential: that can't resolve drops the binding — never 'KEY=None'."""
    monkeypatch.setattr(session_launcher, "_resolve_credential", lambda key: None)
    _run(output_dir=str(tmp_path / "run"),
         extra_env={"GH_TOKEN": "credential:github.token"})
    joined = " ".join(captured_run[0])
    assert "GH_TOKEN" not in joined


def test_codex_interactive_does_not_require_claude_credentials(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    out = _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        image="autonomy-session-platform",
    )
    assert out == "fake-container-id"
    cmd = captured_run[0]
    assert "codex" in cmd


def test_codex_trust_uses_dedicated_working_repo_mount(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch,
    platform_snapshot,
):
    """A non-Autonomy repo must not hide or trust the platform checkout."""
    worktree = tmp_path / "idea-board-worktree"
    worktree.mkdir()
    captured: dict = {}

    def fake_tool_mounts(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(
        session_launcher, "_resolve_optional_tool_mounts", fake_tool_mounts,
    )
    _run(
        output_dir=str(tmp_path / "run"),
        harness="codex",
        mounts={str(worktree): "/workspace/idea-board"},
        working_dir="/workspace/idea-board",
    )

    assert captured["worktree_host"] == worktree
    cmd = captured_run[0]
    joined = " ".join(cmd)
    assert f"{worktree}:/workspace/idea-board" in joined
    assert f"{platform_snapshot.path}:/workspace/repo:ro" in joined
    assert cmd[cmd.index("-w") + 1] == "/workspace/idea-board"


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
        image="autonomy-session-platform",
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
        image="autonomy-session-platform",
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
        implementation_version=2,
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


def test_capability_skill_installed_at_claude_discovery_path(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """The SKILL.md of an enabled capability is copied per-session and mounted
    where Claude Code actually discovers skills — the harness's personal
    skills dir ~/.claude/skills/<name>/ — under its frontmatter name. The
    personal dir is writable and cwd-independent on every workspace (unlike a
    hardcoded /workspace/repo, which is read-only or not the cwd on some)."""
    _run(
        output_dir=str(tmp_path / "run"),
        capabilities=(_github_capability(),),
    )
    docker_cmd = next(c for c in captured_run if c and c[0] == "docker")
    mounts = _mounts(docker_cmd)
    assert any(
        m.endswith("cap-skills/github:/home/agent/.claude/skills/github:ro")
        for m in mounts
    )
    installed = tmp_path / "run" / "cap-skills" / "github" / "SKILL.md"
    fm = session_launcher._skill_frontmatter(installed.read_text())
    assert fm["name"] == "github" and fm["description"]


def test_capability_skill_appends_org_primer_to_session_copy(tmp_path, monkeypatch):
    import dataclasses

    monkeypatch.setattr(session_launcher, "REPO_ROOT", tmp_path)
    cap_dir = tmp_path / "cap"
    cap_dir.mkdir()
    (cap_dir / "SKILL.md").write_text(
        "---\nname: jira\ndescription: Jira tools\n---\n\n# Base skill\n"
    )
    cap = dataclasses.replace(
        _github_capability(),
        implementation="autonomy/jira",
        skill_path="cap/SKILL.md",
        org_primer="Target Fix Versions is `customfield_10172`.",
    )

    mounts = session_launcher._capability_skill_surface(
        [cap], tmp_path / "run", "claude",
    )
    installed = tmp_path / "run" / "cap-skills" / "jira" / "SKILL.md"
    text = installed.read_text()
    assert mounts[str(installed.parent)] == "/home/agent/.claude/skills/jira:ro"
    assert text.index("# Base skill") < text.index("## Organization-specific guidance")
    assert "customfield_10172" in text


def test_capability_skill_requires_frontmatter_and_slug_name(tmp_path, monkeypatch):
    """A SKILL.md the harness would silently ignore is not installed: missing
    name/description frontmatter, an unterminated block, or a non-slug name
    (e.g. vendor/product) all skip with a warning instead of a dead mount."""
    import dataclasses
    monkeypatch.setattr(session_launcher, "REPO_ROOT", tmp_path)
    cap_dir = tmp_path / "cap"
    cap_dir.mkdir()
    run_dir = tmp_path / "run"

    def cap_with(skill_text):
        (cap_dir / "SKILL.md").write_text(skill_text)
        return dataclasses.replace(_github_capability(), skill_path="cap/SKILL.md")

    no_fm = cap_with("# Just a doc\nno frontmatter here\n")
    assert session_launcher._capability_skill_surface([no_fm], run_dir, "claude") == {}

    bad_name = cap_with("---\nname: vendor/product\ndescription: d\n---\nbody\n")
    assert session_launcher._capability_skill_surface([bad_name], run_dir, "claude") == {}

    no_desc = cap_with("---\nname: good-name\n---\nbody\n")
    assert session_launcher._capability_skill_surface([no_desc], run_dir, "claude") == {}

    good = cap_with("---\nname: good-name\ndescription: does things\n---\nbody\n")
    mounts = session_launcher._capability_skill_surface([good], run_dir, "claude")
    assert list(mounts.values()) == ["/home/agent/.claude/skills/good-name:ro"]
    # codex projection is a deliberate gap (host-home bind) — nothing installed
    assert session_launcher._capability_skill_surface([good], run_dir, "codex") == {}


def test_all_checked_in_capability_skills_load(tmp_path):
    """Every capability SKILL.md in the repo satisfies the harness contract —
    frontmatter with name (plain slug) + description."""
    skills = sorted(
        (session_launcher.REPO_ROOT / "agents" / "capabilities").glob("*/SKILL.md"))
    assert skills, "expected at least one capability SKILL.md"
    for skill in skills:
        fm = session_launcher._skill_frontmatter(skill.read_text())
        assert fm, f"{skill} has no frontmatter block"
        assert fm.get("name") and fm.get("description"), f"{skill} missing keys"
        assert session_launcher._SKILL_NAME_RE.match(fm["name"]), \
            f"{skill} name {fm['name']!r} is not a plain slug"


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
    the bundle and synthesizes no CAPABILITY shims — the shim directory now
    always exists carrying exactly the bd close gate (deliberate change,
    auto-w41na: the gate rides every session)."""
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
    # The shim dir exists for the gate alone: no capability commands leak in.
    assert any(":/etc/autonomy/cap-bin:" in m for m in mounts)
    assert any(e.startswith("AUTONOMY_CAPABILITY_BIN=") for e in envs)
    shim_dir = run_dir / "cap-bin"
    assert sorted(p.name for p in shim_dir.iterdir()) == ["bd"]


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


# ── auto-08n3f: substrate-backed multi-token Claude auth ────────────


class _FakeRow:
    """Stand-in for substrate ``ResolvedSetting`` used by the picker tests.

    Carries just the fields the launcher reads (``key``, ``payload``,
    ``created_at``) so we can compose deterministic substrate states
    without spinning up a real graph DB.
    """

    def __init__(
        self, *, key: str, payload: dict,
        created_at: str = "2026-05-06T00:00:00Z",
    ) -> None:
        self.key = key
        self.payload = payload
        self.created_at = created_at


def _setup_token_row(org_uuid: str, raw_key: str) -> _FakeRow:
    return _FakeRow(key=org_uuid, payload={"raw_key": raw_key})


def _credentials_row(org_uuid: str, alias: str) -> _FakeRow:
    return _FakeRow(
        key=org_uuid,
        payload={
            "alias": alias,
            "organization_name": f"{alias}-org",
            "account_email": f"{alias}@example.com",
            "access_token": f"sk-ant-oat01-access-{alias}",
            "refresh_token": f"sk-ant-ort01-refresh-{alias}",
            "expires_at_ms": 9999999999999,
            "scopes": ["user:profile"],
        },
    )


_FRESH_USAGE_TS = "2026-05-06T00:00:00Z"


def _usage_payload(
    org_uuid: str, alias: str, *,
    short_pct: float, long_pct: float,
    updated_at: str = _FRESH_USAGE_TS,
) -> dict:
    return {
        "harness": "claude",
        "account_id": org_uuid,
        "alias": alias,
        "updated_at": updated_at,
        "windows": {
            "short": {"used_percent": short_pct},
            "long": {"used_percent": long_pct},
        },
    }


@pytest.fixture
def freeze_now(monkeypatch):
    """Pin ``datetime.now(timezone.utc)`` inside session_launcher."""
    from datetime import datetime, timezone

    fixed = datetime(2026, 5, 6, 0, 0, 0, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(session_launcher, "datetime", _FrozenDatetime)
    return fixed


class TestSubstrateCredentialsPicker:
    """Acceptance criteria for ``_resolve_credentials_via_substrate``.

    Three scenarios from the bead spec:
    (a) all setup tokens have fresh harness-usage rows → max-min pick is
        deterministic.
    (b) some tokens lack fresh usage rows → uniform random pick.
    (c) zero unexpired setup-token rows → calls ``graph claude install``.
    """

    def test_a_maxmin_pick_is_deterministic(self, monkeypatch, freeze_now):
        tokens = [
            _setup_token_row("org-A", "raw-A"),
            _setup_token_row("org-B", "raw-B"),
        ]
        creds = [
            _credentials_row("org-A", "gmail"),
            _credentials_row("org-B", "auto-network"),
        ]
        usage = [
            # B has more headroom on the short window (95% free vs 20%);
            # max-min picks B.
            _usage_payload("org-A", "gmail",
                           short_pct=80.0, long_pct=10.0),
            _usage_payload("org-B", "auto-network",
                           short_pct=5.0, long_pct=10.0),
        ]
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: tokens)
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: creds)
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: usage)

        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None,
        )

        assert result == {
            "type": "token",
            "token": "raw-B",
            "harness_token": "org-B",
            "alias": "auto-network",
        }

    def test_a_repeats_pick_across_calls(self, monkeypatch, freeze_now):
        """Same usage state → same pick. Determinism (no rng involvement)."""
        tokens = [
            _setup_token_row("org-A", "raw-A"),
            _setup_token_row("org-B", "raw-B"),
        ]
        creds = [
            _credentials_row("org-A", "gmail"),
            _credentials_row("org-B", "auto-network"),
        ]
        usage = [
            _usage_payload("org-A", "gmail",
                           short_pct=80.0, long_pct=10.0),
            _usage_payload("org-B", "auto-network",
                           short_pct=5.0, long_pct=10.0),
        ]
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: tokens)
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: creds)
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: usage)

        # Force the rng to a state that would prefer A if the random
        # branch fired — the test should still pick B because all tokens
        # have fresh telemetry, so the deterministic branch runs.
        rng = __import__("random").Random(0)
        first = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None, rng=rng,
        )
        second = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None, rng=rng,
        )
        assert first == second
        assert first["harness_token"] == "org-B"

    def test_b_random_pick_uniform_when_some_lack_usage(
        self, monkeypatch, freeze_now,
    ):
        """B token has no harness-usage row → fall through to random pick.

        We seed the rng so the choice is reproducible and assert it
        returns one of the two tokens. The acceptance criterion is
        "random pick chosen uniformly" — we exercise both branches by
        calling with two distinct seeds.
        """
        tokens = [
            _setup_token_row("org-A", "raw-A"),
            _setup_token_row("org-B", "raw-B"),
        ]
        creds = [
            _credentials_row("org-A", "gmail"),
            _credentials_row("org-B", "auto-network"),
        ]
        # Only org-A has a usage row — org-B has nothing → random branch.
        usage = [
            _usage_payload("org-A", "gmail",
                           short_pct=10.0, long_pct=10.0),
        ]
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: tokens)
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: creds)
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: usage)

        import random as _random
        results = set()
        for seed in range(20):
            r = _random.Random(seed)
            picked = session_launcher._resolve_credentials_via_substrate(
                prefer_alias=None, rng=r,
            )
            assert picked is not None
            results.add(picked["harness_token"])
        # Both tokens must appear over a small range of seeds — proves
        # the picker is drawing uniformly rather than always returning
        # the first token.
        assert results == {"org-A", "org-B"}

    def test_b_random_pick_when_no_usage_at_all(self, monkeypatch, freeze_now):
        """First-launch / empty harness-usage set → random branch fires."""
        tokens = [
            _setup_token_row("org-A", "raw-A"),
            _setup_token_row("org-B", "raw-B"),
        ]
        creds = [
            _credentials_row("org-A", "gmail"),
            _credentials_row("org-B", "auto-network"),
        ]
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: tokens)
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: creds)
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: [])

        import random as _random
        # Force the rng output to org-B so we can assert exactly.
        class _PinnedRng:
            def choice(self, seq):
                return seq[1]

        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None, rng=_PinnedRng(),
        )
        assert result["harness_token"] == "org-B"
        assert result["alias"] == "auto-network"

    def test_b_random_pick_when_usage_is_stale(self, monkeypatch, freeze_now):
        """Usage rows older than the schema TTL count as "no usage" → random."""
        tokens = [
            _setup_token_row("org-A", "raw-A"),
            _setup_token_row("org-B", "raw-B"),
        ]
        creds = [
            _credentials_row("org-A", "gmail"),
            _credentials_row("org-B", "auto-network"),
        ]
        usage = [
            _usage_payload(
                "org-A", "gmail",
                short_pct=10.0, long_pct=10.0,
                updated_at="2025-01-01T00:00:00Z",  # ancient
            ),
            _usage_payload(
                "org-B", "auto-network",
                short_pct=10.0, long_pct=10.0,
                updated_at="2025-01-01T00:00:00Z",
            ),
        ]
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: tokens)
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: creds)
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: usage)

        class _PinnedRng:
            def choice(self, seq):
                return seq[0]

        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None, rng=_PinnedRng(),
        )
        # Stale usage means we hit the random branch and the rng wins —
        # not the (would-be-) max-min pick on the stale numbers.
        assert result["harness_token"] == "org-A"

    def test_c_zero_tokens_calls_graph_claude_install(
        self, monkeypatch, freeze_now,
    ):
        """No unexpired setup-token rows → call install, then re-read."""
        # First call: empty. After install: one row appears.
        states = [[], [_setup_token_row("org-A", "raw-A")]]

        def _fake_setup_tokens():
            return states.pop(0)

        install_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            install_calls.append(list(cmd))

            class _R:
                returncode = 0
            return _R()

        monkeypatch.setattr(session_launcher, "_setup_token_rows", _fake_setup_tokens)
        monkeypatch.setattr(
            session_launcher, "_credentials_rows",
            lambda: [_credentials_row("org-A", "gmail")],
        )
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: [])
        monkeypatch.setattr(session_launcher.subprocess, "run", _fake_run)

        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None,
        )

        assert install_calls == [["graph", "claude", "install"]]
        assert result is not None
        assert result["harness_token"] == "org-A"
        assert result["alias"] == "gmail"

    def test_c_install_failure_returns_none(self, monkeypatch, freeze_now):
        """If install also yields nothing, return None so the launcher
        surfaces "No Claude credentials found" the way it does today."""
        import subprocess as _sp

        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: [])
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: [])
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: [])

        def _fake_run(cmd, **kwargs):
            raise _sp.CalledProcessError(1, cmd)

        monkeypatch.setattr(session_launcher.subprocess, "run", _fake_run)

        assert (
            session_launcher._resolve_credentials_via_substrate(prefer_alias=None)
            is None
        )

    def test_prefer_alias_resolves_to_matching_org(
        self, monkeypatch, freeze_now,
    ):
        """Operator override picks the credentials row whose alias matches,
        then the setup-token row keyed by the same org UUID."""
        tokens = [
            _setup_token_row("org-A", "raw-A"),
            _setup_token_row("org-B", "raw-B"),
        ]
        creds = [
            _credentials_row("org-A", "gmail"),
            _credentials_row("org-B", "auto-network"),
        ]
        usage = [
            _usage_payload("org-A", "gmail",
                           short_pct=5.0, long_pct=5.0),
            _usage_payload("org-B", "auto-network",
                           short_pct=80.0, long_pct=80.0),
        ]
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: tokens)
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: creds)
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: usage)

        # auto-network wins despite gmail having more headroom because
        # the operator override is authoritative.
        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias="auto-network",
        )
        assert result["harness_token"] == "org-B"
        assert result["alias"] == "auto-network"

    def test_prefer_unknown_alias_falls_through_to_selection(
        self, monkeypatch, freeze_now,
    ):
        """A typo / unknown alias does not block dispatch — the picker
        just falls through to the headroom-based or random selection."""
        tokens = [_setup_token_row("org-A", "raw-A")]
        creds = [_credentials_row("org-A", "gmail")]
        usage = [
            _usage_payload("org-A", "gmail",
                           short_pct=10.0, long_pct=10.0),
        ]
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: tokens)
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: creds)
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: usage)

        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias="ghost",
        )
        assert result["harness_token"] == "org-A"

    def test_expired_setup_token_rows_filtered(self, monkeypatch, freeze_now):
        """Setup-token rows past their @cache TTL (1y) are dropped before
        the picker considers them."""
        from datetime import timedelta
        from tools.graph.schemas.claude_setup_tokens import (
            CLAUDE_SETUP_TOKEN_TTL,
        )
        # One fresh row (created today) + one expired row (created 2y ago).
        fresh = _FakeRow(
            key="org-A",
            payload={"raw_key": "raw-A"},
            created_at=freeze_now.isoformat(),
        )
        expired_at = freeze_now - CLAUDE_SETUP_TOKEN_TTL - timedelta(days=1)
        old = _FakeRow(
            key="org-B",
            payload={"raw_key": "raw-B"},
            created_at=expired_at.isoformat(),
        )

        # Stand in for ops.read_set: return both rows; _setup_token_rows
        # itself does the filtering, so let it run unmocked.
        from types import SimpleNamespace

        def _fake_read_set(set_id, org=None, peers=None):
            return SimpleNamespace(members=[fresh, old])

        import tools.graph.ops as _ops
        monkeypatch.setattr(_ops, "read_set", _fake_read_set)
        monkeypatch.setattr(
            session_launcher, "_credentials_rows",
            lambda: [_credentials_row("org-A", "gmail")],
        )
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: [])

        class _PinnedRng:
            def choice(self, seq):
                # The picker should only see the fresh row, so seq[0] is org-A.
                assert len(seq) == 1
                return seq[0]

        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None, rng=_PinnedRng(),
        )
        assert result["harness_token"] == "org-A"


class TestResolveCredentials:
    def test_env_var_wins_and_emits_no_alias(self, tmp_path, monkeypatch):
        """Acceptance criterion: ``CLAUDE_CODE_OAUTH_TOKEN`` env-var
        override path preserved (operator backstop when substrate is
        unavailable / hand-launched runs)."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok-xyz")

        creds = session_launcher._resolve_credentials()
        assert creds == {"type": "token", "token": "env-tok-xyz"}
        assert "alias" not in creds

    def test_returns_none_when_substrate_empty(self, monkeypatch):
        """Empty substrate, install also fails → None."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: [])

        def _bail(cmd, **kwargs):
            import subprocess as _sp
            raise _sp.CalledProcessError(1, cmd)

        monkeypatch.setattr(session_launcher.subprocess, "run", _bail)

        assert session_launcher._resolve_credentials() is None


def test_meta_doc_records_harness_token(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    """auto-08n3f: launcher writes the chosen Anthropic org UUID into
    ``.session_meta.json`` under ``harness_token`` so the dashboard can
    join it to the friendly alias on registration."""
    monkeypatch.setattr(
        session_launcher,
        "_resolve_credentials",
        lambda: {
            "type": "token",
            "token": "tok-xyz",
            "alias": "primary",
            "harness_token": "879d3b11-f034-4e6e-87b2-6a8f839f4779",
        },
    )
    run_dir = tmp_path / "run"
    _run(output_dir=str(run_dir))

    meta = json.loads((run_dir / "sessions" / ".session_meta.json").read_text())
    assert meta["harness_token"] == "879d3b11-f034-4e6e-87b2-6a8f839f4779"


def test_meta_doc_omits_token_when_none(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    """No ``harness_token`` on the creds dict (env-var compat path) →
    no ``harness_token`` key in the meta doc."""
    monkeypatch.setattr(
        session_launcher,
        "_resolve_credentials",
        lambda: {"type": "token", "token": "tok-xyz"},
    )
    run_dir = tmp_path / "run"
    _run(output_dir=str(run_dir))

    meta = json.loads((run_dir / "sessions" / ".session_meta.json").read_text())
    assert "harness_token" not in meta


def test_no_hardcoded_license_mount(tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch):
    """The ad-hoc host license.yaml overlay must be gone.

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


# ── platform mount: snapshot instead of live host root (auto-j3oj3) ──

def _mount_specs(cmd: list[str]) -> list[str]:
    """Every ``-v`` argument value in the docker command."""
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]


def test_default_platform_mount_is_snapshot_not_live_root(
    tmp_path, fake_creds, fake_crosstalk, captured_run, platform_snapshot,
):
    """No caller mount at /workspace/repo → the git snapshot is mounted ro,
    and the live host root appears nowhere in the docker command."""
    _run(output_dir=str(tmp_path / "run"))
    specs = _mount_specs(captured_run[0])
    assert f"{platform_snapshot.path}:/workspace/repo:ro" in specs
    assert platform_snapshot.calls, "launcher never asked for the snapshot"
    live_root = f"{session_launcher.REPO_ROOT}:"
    assert not any(s.startswith(live_root) for s in specs), specs


def test_caller_workspace_repo_mount_skips_snapshot(
    tmp_path, fake_creds, fake_crosstalk, captured_run, platform_snapshot,
):
    """A workspace repo at /workspace/repo displaces the default entirely —
    the launcher must not even prepare a snapshot."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _run(
        output_dir=str(tmp_path / "run"),
        mounts={str(worktree): "/workspace/repo"},
    )
    specs = _mount_specs(captured_run[0])
    assert f"{worktree}:/workspace/repo" in specs
    assert not platform_snapshot.calls
    assert sum(s.split(":")[1] == "/workspace/repo" if s.count(":") >= 2
               else False for s in specs) <= 1


def test_snapshot_failure_launches_without_platform_mount(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch,
):
    """Snapshot prep failure must NOT fall back to the live host root, and
    must also skip the uploads bind (its mount point has no parent)."""
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot",
                        lambda: None)
    result = _run(output_dir=str(tmp_path / "run"))
    assert result is not None  # the fleet still starts
    specs = _mount_specs(captured_run[0])
    container_targets = [s.split(":")[1] for s in specs]
    assert not any(
        t == "/workspace/repo" or t.startswith("/workspace/repo/")
        for t in container_targets
    )
    live_root = f"{session_launcher.REPO_ROOT}:"
    assert not any(s.startswith(live_root) for s in specs)


def test_stale_graph_db_mount_is_gone(
    tmp_path, fake_creds, fake_crosstalk, captured_run, platform_snapshot,
):
    """Nothing in-container reads the pre-sharding graph.db; the mount is dropped."""
    _run(output_dir=str(tmp_path / "run"))
    assert not any("graph.db" in s for s in _mount_specs(captured_run[0]))


def test_each_session_mounts_only_its_exact_secret_ramfs_subdir(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch,
):
    """The dashboard may see the delivery root; a session must never see it."""
    from agents import secret_ramfs

    monkeypatch.setattr(
        secret_ramfs,
        "provision_session_dir",
        lambda name, uid: f"/run/autonomy-secrets/{name}",
    )
    _run(name="auto-a", output_dir=str(tmp_path / "run-a"))
    _run(name="auto-b", output_dir=str(tmp_path / "run-b"))

    mounts_a = [
        captured_run[0][i + 1]
        for i, token in enumerate(captured_run[0][:-1])
        if token == "--mount"
    ]
    mounts_b = [
        captured_run[1][i + 1]
        for i, token in enumerate(captured_run[1][:-1])
        if token == "--mount"
    ]
    assert "type=bind,src=/run/autonomy-secrets/auto-a,dst=/run/secrets" in mounts_a
    assert "type=bind,src=/run/autonomy-secrets/auto-b,dst=/run/secrets" in mounts_b
    assert not any("autonomy-secrets/auto-b" in mount for mount in mounts_a)
    assert not any("autonomy-secrets/auto-a" in mount for mount in mounts_b)
    assert not any(
        mount in {
            "type=bind,src=/run/autonomy-secrets,dst=/run/secrets",
            "type=bind,src=/run/autonomy-secrets,dst=/run/autonomy-secrets",
        }
        for mount in mounts_a + mounts_b
    )


def test_uploads_dir_mounted_read_only_for_file_handoff(
    tmp_path, fake_creds, fake_crosstalk, captured_run, platform_snapshot,
):
    """POST /api/upload handoff path stays readable wherever its mount point
    exists — the snapshot (tracked .gitkeep) and platform worktrees."""
    _run(output_dir=str(tmp_path / "run"))
    uploads = f"{session_launcher.REPO_ROOT / 'data' / 'uploads'}:/workspace/repo/data/uploads:ro"
    assert uploads in _mount_specs(captured_run[0])

    worktree = tmp_path / "wt2"
    (worktree / "data" / "uploads").mkdir(parents=True)
    _run(output_dir=str(tmp_path / "run2"),
         mounts={str(worktree): "/workspace/repo"})
    assert uploads in _mount_specs(captured_run[1])


def test_uploads_bind_skipped_when_mount_point_missing(
    tmp_path, fake_creds, fake_crosstalk, captured_run, platform_snapshot,
):
    """A repo at /workspace/repo without data/uploads must lose the uploads
    bind, not the launch: runc cannot mkdir a mount point under a read-only
    parent, so a blind nested bind is a deterministic fleet-stopping OCI
    failure (caught by the auto-j3oj3 pre-merge smoke)."""
    worktree = tmp_path / "foreign-repo"
    worktree.mkdir()
    result = _run(output_dir=str(tmp_path / "run"),
                  mounts={str(worktree): "/workspace/repo:ro"})
    assert result is not None
    specs = _mount_specs(captured_run[0])
    assert f"{worktree}:/workspace/repo:ro" in specs
    assert not any("/workspace/repo/data/uploads" in s for s in specs)


def test_beads_credential_key_is_masked(
    tmp_path, fake_creds, fake_crosstalk, captured_run, platform_snapshot,
):
    """.beads stays read-write for bd, but the dolt credential inside it is
    shadowed by an empty read-only bind in every container.

    Sourced from the STATE volume: the code volume has no .beads on a fresh
    node, so mounting it from there created no container at all (auto-qk4ip).
    """
    _run(output_dir=str(tmp_path / "run"))
    specs = _mount_specs(captured_run[0])
    assert f"{session_launcher.DATA_ROOT / '.beads'}:/data/.beads" in specs
    assert "/dev/null:/data/.beads/.beads-credential-key:ro" in specs


# ── Codex credential cutover (bead auto-l1h3f) ───────────────────────

def _codex_row(key, payload):
    from types import SimpleNamespace
    return SimpleNamespace(key=key, payload=payload)


def _fresh_codex_payload(**over):
    p = {
        "email": "codexuser@example.com",
        "auth_mode": "chatgpt",
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "id_token": "id-1",
        "expires_at_ms": 4102444800000,
        "last_refresh_at": "2026-08-14T00:00:00Z",
    }
    p.update(over)
    return p


def test_materialize_codex_auth_json_reconstructs_file(tmp_path, monkeypatch):
    """The substrate row is rebuilt into the on-disk auth.json shape Codex expects."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(
        session_launcher, "_codex_credential_rows",
        lambda: [_codex_row("acct-UUID", _fresh_codex_payload())],
    )
    out = session_launcher._materialize_codex_auth_json(run_dir)
    assert out is not None
    doc = json.loads(Path(out).read_text())
    assert doc["auth_mode"] == "chatgpt"
    assert doc["OPENAI_API_KEY"] is None
    # account_id is the row KEY, not a payload field
    assert doc["tokens"]["account_id"] == "acct-UUID"
    assert doc["tokens"]["access_token"] == "at-1"
    assert doc["tokens"]["refresh_token"] == "rt-1"
    assert doc["tokens"]["id_token"] == "id-1"
    assert doc["last_refresh"] == "2026-08-14T00:00:00Z"
    # 0600 like the host file
    assert (Path(out).stat().st_mode & 0o777) == 0o600


def test_materialize_codex_auth_json_missing_row_returns_none(tmp_path, monkeypatch):
    """No usable substrate row → no file → Codex simply unavailable (truthful)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(session_launcher, "_codex_credential_rows", lambda: [])
    assert session_launcher._materialize_codex_auth_json(run_dir) is None


def test_materialize_codex_auth_json_missing_row_warns_with_remedy(
    tmp_path, monkeypatch, caplog,
):
    """A None return must WARN with the remedy — a missed migration is an
    operator-visible error, not a silent sign-in prompt at launch."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(session_launcher, "_codex_credential_rows", lambda: [])
    with caplog.at_level("INFO", logger=session_launcher.logger.name):
        assert session_launcher._materialize_codex_auth_json(run_dir) is None
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warns) == 1
    msg = warns[0].getMessage()
    assert "no usable Codex credential row" in msg
    assert "graph credentials import" in msg


def test_pick_codex_credential_row_prefers_freshest(monkeypatch):
    older = _codex_row("a", _fresh_codex_payload(expires_at_ms=1000))
    newer = _codex_row("b", _fresh_codex_payload(expires_at_ms=9000))
    incomplete = _codex_row("c", {"auth_mode": "chatgpt"})  # no tokens
    assert session_launcher._pick_codex_credential_row(
        [older, newer, incomplete]) is newer
    assert session_launcher._pick_codex_credential_row([incomplete]) is None


def test_optional_tool_mounts_uses_substrate_not_host_auth_json(
    tmp_path, monkeypatch,
):
    """The credential mount is the materialized substrate file, never ~/.codex/auth.json."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(
        session_launcher, "_codex_credential_rows",
        lambda: [_codex_row("acct-UUID", _fresh_codex_payload())],
    )
    mounts = session_launcher._resolve_optional_tool_mounts(run_dir=run_dir)
    # Exactly one mount targets the container auth.json path...
    auth_hosts = [
        hp for hp, spec in mounts.items()
        if spec.split(":")[0] == "/home/agent/.codex/auth.json"
    ]
    assert len(auth_hosts) == 1
    # ...and it is the run_dir copy, NOT the operator's host file.
    host_auth = str(Path.home() / ".codex" / "auth.json")
    assert auth_hosts[0] != host_auth
    assert auth_hosts[0].startswith(str(run_dir))


def test_optional_tool_mounts_no_row_mounts_no_auth(tmp_path, monkeypatch):
    """A missing substrate row leaves no auth.json mount at all."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(session_launcher, "_codex_credential_rows", lambda: [])
    mounts = session_launcher._resolve_optional_tool_mounts(run_dir=run_dir)
    assert not any(
        spec.split(":")[0] == "/home/agent/.codex/auth.json"
        for spec in mounts.values()
    )


def test_launcher_source_has_no_host_codex_auth_json_read():
    """Grep-level acceptance: no launcher code path reads the host ~/.codex/auth.json.

    The retired construct was ``host_codex_home / "auth.json"`` — the only
    code that ever pointed a mount at the operator's on-disk credential.
    Prose references to the path in docstrings are fine; the *code* read is
    what must be gone, and its container-target string
    (``/home/agent/.codex/auth.json``) is the materialized-from-substrate
    mount, not a host read.
    """
    import inspect
    src = inspect.getsource(session_launcher)
    assert 'host_codex_home / "auth.json"' not in src
    assert "host_codex_home / 'auth.json'" not in src


def test_codex_auth_copy_cleanup_is_scheduled(
    tmp_path, fake_crosstalk, captured_run, platform_snapshot, monkeypatch,
):
    """The materialized Codex auth.json (live tokens) is cleaned up post-exit."""
    monkeypatch.setattr(
        session_launcher, "_codex_credential_rows",
        lambda: [_codex_row("acct-UUID", _fresh_codex_payload())],
    )
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        session_launcher, "_schedule_creds_cleanup",
        lambda cid, path: scheduled.append((cid, path)),
    )
    _run(output_dir=str(tmp_path / "run"), harness="codex")
    # exactly one cleanup, for the run_dir codex-auth.json copy
    assert len(scheduled) == 1
    assert scheduled[0][1].endswith("codex-auth.json")
    assert str(tmp_path / "run") in scheduled[0][1]


def test_every_session_gets_the_bd_close_gate(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """The golden-rule close gate (auto-w41na) rides EVERY session's cap-bin
    — no capability opt-in required — as a byte-identical copy of
    tools/beads/bd (a copy, never an exec-wrapper: a wrapper would defeat
    the shim's resolved-path self-location and loop)."""
    import stat as _stat

    run_dir = tmp_path / "run"
    _run(output_dir=str(run_dir), capabilities=())
    gate = run_dir / "cap-bin" / "bd"
    assert gate.is_file(), "bd gate missing from cap-bin"
    assert gate.stat().st_mode & _stat.S_IXUSR
    repo_shim = (
        Path(__file__).resolve().parents[2] / "tools" / "beads" / "bd"
    )
    assert gate.read_text() == repo_shim.read_text(encoding="utf-8")
    # And the surface is mounted + exposed even with zero capabilities.
    cmd = captured_run[0]
    mounts = _mounts(cmd)
    assert f"{run_dir / 'cap-bin'}:/etc/autonomy/cap-bin:ro" in mounts


def _agent_test_capability() -> MaterializedCapability:
    return MaterializedCapability(
        contract="test_execution",
        contract_version=1,
        implementation="autonomy/agent-test",
        implementation_version=1,
        delivery_mode="mounted_tools",
        package_root="agents/capabilities/agent_test",
        mount_target=f"{CAPABILITIES_MOUNT_DIR}/autonomy-agent-test",
        tool_paths=(),
        primer_path="agents/capabilities/agent_test/primer.md",
        skill_path="agents/capabilities/agent_test/SKILL.md",
        tool_target=CapabilityToolTarget(
            source="tools/agent_test",
            target="/opt/agent_test",
            expose_commands=("agent-test", "pytest", "py.test"),
        ),
    )


def test_capability_mounts_refuses_external_tool_path_before_docker() -> None:
    """Legacy invalid settings fail by name instead of reaching runc."""
    cap = _agent_test_capability()
    invalid = MaterializedCapability(
        **{
            **cap.__dict__,
            "tool_paths": ("tools/agent_test",),
        }
    )
    with pytest.raises(ValueError) as exc:
        session_launcher._capability_mounts((invalid,))
    message = str(exc.value)
    assert "outside package_root" in message
    assert "tool_target" in message


def test_agent_test_capability_exposes_cli_and_refuses_raw_pytest_commands(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    run_dir = tmp_path / "run"
    _run(output_dir=str(run_dir), capabilities=(_agent_test_capability(),))

    agent_test = run_dir / "cap-bin" / "agent-test"
    assert agent_test.is_file()
    assert agent_test.stat().st_mode & 0o100
    assert "/opt/agent_test/agent-test" in agent_test.read_text()
    mounts = _mounts(captured_run[0])
    runtime_source = Path(__file__).resolve().parents[2] / "tools/agent_test"
    assert (
        f"{runtime_source}:/opt/agent_test:ro"
        in mounts
    )
    package_mount = (
        Path(__file__).resolve().parents[2] / "agents/capabilities/agent_test"
    )
    assert (
        f"{package_mount}:{CAPABILITIES_MOUNT_DIR}/autonomy-agent-test:ro"
        in mounts
    )
    source = Path(__file__).resolve().parents[2] / "tools/agent_test/agent-test"
    assert "/opt/agent_test" in source.read_text()
    process = subprocess.Popen(
        [str(source), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        },
    )
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert stdout.strip() == "0.5.1"

    for command in ("pytest", "py.test"):
        gate = run_dir / "cap-bin" / command
        assert gate.is_file()
        assert gate.stat().st_mode & 0o100
        assert f"/opt/agent_test/{command}" in gate.read_text()
        gate_source = Path(__file__).resolve().parents[2] / f"tools/agent_test/{command}"
        if command == "py.test":
            assert "exec /opt/agent_test/pytest" in gate_source.read_text()
            continue
        process = subprocess.Popen(
            [str(gate_source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "AUTONOMY_SESSION": ""},
        )
        _stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 64
        assert "Direct pytest is disabled" in stderr


def test_session_without_test_execution_capability_has_no_test_commands(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    run_dir = tmp_path / "run"
    _run(output_dir=str(run_dir), capabilities=())
    assert not (run_dir / "cap-bin" / "agent-test").exists()
    assert not (run_dir / "cap-bin" / "pytest").exists()
    assert not (run_dir / "cap-bin" / "py.test").exists()


def test_golden_mount_argv_is_byte_identical(
    tmp_path, fake_creds, fake_crosstalk, captured_run, monkeypatch,
):
    """GOLDEN (bead auto-vm8qh, criterion 1): pins the COMPLETE docker-run argv
    launch_session emits for a representative host-process launch, and asserts the
    declare->resolve->emit refactor reproduces it byte-for-byte. Hermetic: the
    platform roots, snapshot, optional-tool mounts and the session token are all
    pinned, and REPO_ROOT is deliberately NOT /workspace/repo, so normalizing host
    source prefixes can never rewrite a fixed container destination."""
    from agents import secret_ramfs

    repo = tmp_path / "repo"
    data = repo / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(session_launcher, "REPO_ROOT", repo)
    monkeypatch.setattr(session_launcher, "DATA_ROOT", data)
    snap = tmp_path / "snap"
    (snap / "data" / "uploads").mkdir(parents=True)
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: str(snap))
    monkeypatch.setattr(
        session_launcher, "_resolve_optional_tool_mounts",
        lambda **kw: {str(tmp_path / "codex-config.toml"): "/home/agent/.codex/config.toml:ro"},
    )
    monkeypatch.setattr(session_launcher.secrets, "token_urlsafe", lambda n=32: "TOKEN")
    monkeypatch.setattr(
        secret_ramfs,
        "provision_session_dir",
        lambda name, uid: f"/run/autonomy-secrets/{name}",
    )
    run_dir = tmp_path / "run"
    gmd = tmp_path / "CLAUDE.md"; gmd.write_text("primer")
    startup = tmp_path / "startup.sh"; startup.write_text("#!/bin/sh\n")
    ws_src = tmp_path / "wsmount"; ws_src.mkdir()

    session_launcher.launch_session(
        session_type="dispatch", name="test-session", prompt=None, detach=True,
        image="session-enterprise", metadata={"org": "test-org"},
        output_dir=str(run_dir), global_claude_md=str(gmd),
        startup_script=str(startup), mounts={str(ws_src): "/opt/data:ro"},
    )
    cmd = list(captured_run[0])

    def norm(tok):  # normalize ONLY dynamic host-source prefixes, never a dest
        for pre, tag in ((str(run_dir), "{RUN}"), (str(snap), "{SNAP}"),
                         (str(data), "{DATA}"), (str(repo), "{REPO}"),
                         (str(tmp_path), "{TMP}")):
            tok = tok.replace(pre, tag)
        return tok
    got = [norm(t) for t in cmd]

    # GOLDEN — the complete pinned argv the launcher emits today. The refactor
    # must reproduce it byte-for-byte; update only when the launch shape
    # intentionally changes.
    expected = [
        "docker", "run", "-d", "--init", "--name", "test-session", "--network=host",
        "-e", "BD_ACTOR=dispatch:test-session",
        "-e", "AUTONOMY_SESSION=test-session",
        "-e", "BD_READONLY=0",
        "-e", "GRAPH_API=https://localhost:8080",
        "-e", "CROSSTALK_TOKEN=TOKEN",
        "-e", "CODEX_HOME=/home/agent/.codex",
        "-e", "CLAUDE_CODE_OAUTH_TOKEN=tok-xyz",
        "-v", "{DATA}/.beads:/data/.beads",
        "-v", "/dev/null:/data/.beads/.beads-credential-key:ro",
        "-v", "{RUN}:/workspace/output",
        "-v", "{RUN}/sessions:/home/agent/.claude/projects",
        "-v", "{TMP}/wsmount:/opt/data:ro",
        "--mount", "type=bind,src=/run/autonomy-secrets/test-session,dst=/run/secrets",
        "-v", "{SNAP}:/workspace/repo:ro",
        "-v", "{DATA}/uploads:/workspace/repo/data/uploads:ro",
        "-v", "{RUN}/cap-bin:/etc/autonomy/cap-bin:ro",
        "-v", "{TMP}/codex-config.toml:/home/agent/.codex/config.toml:ro",
        "-v", "{TMP}/CLAUDE.md:/home/agent/.claude/CLAUDE.md:ro",
        "-v", "{TMP}/CLAUDE.md:/home/agent/.codex/AGENTS.md:ro",
        "-v", "{TMP}/startup.sh:/startup.sh:ro",
        "-e", "AUTONOMY_CAPABILITY_BIN=/etc/autonomy/cap-bin",
        "-w", "/workspace/repo",
        "session-enterprise", "--dangerously-skip-permissions",
        "--model", "claude-opus-4-8[1m]",
    ]
    assert got == expected


def test_build_mount_plan_socket_via_startup_is_refused_at_emit(tmp_path, monkeypatch):
    """Integrated (auto-vm8qh, criterion 3): a startup_script pointing at the
    docker socket goes THROUGH build_mount_plan into the plan, and mount_args
    refuses it over the full plan — closing the bypass those inputs used to have.
    Exercises build_mount_plan with the real input, not just an appended spec."""
    import pytest
    from agents.mount_plan import mount_args, NodeTopology, SocketMountRefused
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    monkeypatch.setattr(session_launcher, "_resolve_optional_tool_mounts", lambda **k: {})
    run_dir = tmp_path / "run"
    plan, _se, _ca = session_launcher.build_mount_plan(
        run_dir=run_dir, sessions_dir=run_dir / "sessions",
        harness="claude", working_dir="/workspace/repo",
        startup_script="/var/run/docker.sock",
    )
    with pytest.raises(SocketMountRefused):
        mount_args(plan, NodeTopology(is_host_process=True))


def _stub_usable_codex_row(monkeypatch):
    """Make a usable Codex credential row exist, so the auth mount is DECLARED —
    without this the declare/materialize distinction has nothing to prove."""
    import types
    monkeypatch.setattr(session_launcher, "_pick_codex_credential_row",
                        lambda rows: types.SimpleNamespace(key="acct", payload={}))
    monkeypatch.setattr(session_launcher, "_codex_credential_rows", lambda: [object()])


def test_declare_mode_declares_but_does_not_materialize_credential(tmp_path, monkeypatch):
    """auto-vm8qh criterion 6: the REAL _resolve_optional_tool_mounts in declare
    mode (materialize_auth=False) references the Codex auth path but writes NO
    credential. Only the materializer + row-pick are stubbed."""
    materialized = []
    monkeypatch.setattr(session_launcher, "_materialize_codex_auth_json",
                        lambda run_dir: materialized.append(run_dir))
    _stub_usable_codex_row(monkeypatch)
    run_dir = tmp_path / "run"; run_dir.mkdir()
    mounts = session_launcher._resolve_optional_tool_mounts(run_dir=run_dir, materialize_auth=False)
    assert materialized == [], "declare mode must NOT write the credential"
    target = str(run_dir / "codex-auth.json")
    assert mounts.get(target) == "/home/agent/.codex/auth.json:ro", "auth mount is still declared"


def test_mount_refusal_mints_no_token_and_materializes_no_credential(tmp_path, fake_creds, monkeypatch):
    """auto-vm8qh criterion 6: a mount refusal returns having minted NO session
    token and materialized NO Codex credential — exercising the REAL declare path
    (a usable row exists, so the auth mount is genuinely declared)."""
    import types
    minted, materialized = [], []
    fake_dao = types.SimpleNamespace(
        auth_db=types.SimpleNamespace(insert_token=lambda *a, **k: minted.append(a)))
    monkeypatch.setitem(__import__("sys").modules, "tools.dashboard.dao", fake_dao)
    monkeypatch.setattr(session_launcher, "_materialize_codex_auth_json",
                        lambda run_dir: materialized.append(run_dir))
    _stub_usable_codex_row(monkeypatch)
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    result = session_launcher.launch_session(
        session_type="dispatch", name="t", prompt=None, detach=True,
        image="x", metadata={"org": "o"}, output_dir=str(tmp_path / "run"),
        mounts={"/var/run/docker.sock": "/data/leak"},   # forces SocketMountRefused
    )
    assert result is None
    assert minted == [], "no session token may be minted on a refused launch"
    assert materialized == [], "no credential may be materialized on a refused launch"


def test_declared_credential_failed_materialization_refuses_before_token(tmp_path, fake_creds, monkeypatch):
    """auto-vm8qh criterion 6: a credential DECLARED at plan time but that fails to
    materialize (write error, or the row expired/raced away) is a launch refusal —
    taken BEFORE the session token is minted, leaving no partial credential file.
    Otherwise the validated argv binds a path that doesn't exist: host-process -v
    would fabricate a dir there, the fallback bind would fail only at docker-run,
    both AFTER the token was minted."""
    import types
    from pathlib import Path
    minted = []
    fake_dao = types.SimpleNamespace(
        auth_db=types.SimpleNamespace(insert_token=lambda *a, **k: minted.append(a)))
    monkeypatch.setitem(__import__("sys").modules, "tools.dashboard.dao", fake_dao)
    _stub_usable_codex_row(monkeypatch)                 # row exists -> auth mount DECLARED
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)

    run_dir = tmp_path / "run"; run_dir.mkdir()
    partial = run_dir / "codex-auth.json"
    def failing_materialize(rd):
        # Simulate a write that landed before failing (or a chmod failure): a file
        # is on disk, but the materializer reports failure by returning None.
        Path(rd).joinpath("codex-auth.json").write_text("partial-credential")
        return None
    monkeypatch.setattr(session_launcher, "_materialize_codex_auth_json", failing_materialize)

    result = session_launcher.launch_session(
        session_type="dispatch", name="t", prompt=None, detach=True,
        image="x", metadata={"org": "o"}, output_dir=str(run_dir),
    )
    assert result is None, "a declared credential that fails to materialize must refuse the launch"
    assert minted == [], "no session token may be minted when materialization failed"
    assert not partial.exists(), "the refusal path must leave no partial credential file behind"


def test_build_mount_plan_socket_via_global_claude_md_is_refused(tmp_path, monkeypatch):
    """Integrated socket refusal for the OTHER bypass input (global_claude_md),
    through build_mount_plan (auto-vm8qh criterion 3/4)."""
    import pytest
    from agents.mount_plan import mount_args, NodeTopology, SocketMountRefused
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    monkeypatch.setattr(session_launcher, "_resolve_optional_tool_mounts", lambda **k: {})
    run_dir = tmp_path / "run"
    plan, _se, _ca = session_launcher.build_mount_plan(
        run_dir=run_dir, sessions_dir=run_dir / "sessions", harness="claude",
        working_dir="/workspace/repo", global_claude_md="/var/run/docker.sock",
    )
    with pytest.raises(SocketMountRefused):
        mount_args(plan, NodeTopology(is_host_process=True))
