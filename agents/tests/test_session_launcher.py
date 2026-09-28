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


@pytest.fixture(autouse=True)
def signin_deliveries(monkeypatch):
    """Record sign-in deliveries instead of running the nsenter helper (and
    its wait for a real container)."""
    got = {"now": [], "background": []}
    monkeypatch.setattr(
        session_launcher, "deliver_signins",
        lambda name, payloads, **_k: got["now"].append((name, sorted(payloads))) or [])
    monkeypatch.setattr(
        session_launcher, "deliver_signins_in_background",
        lambda name, payloads, accounts=None: payloads and got["background"].append(
            (name, sorted(payloads))))
    return got


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
        image="session-widgets",
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
    # Every image's shared entrypoint receives the full harness argv.
    image_index = cmd.index("session-widgets")
    assert cmd[image_index + 1] == "claude"
    assert "--entrypoint" not in cmd
    assert "/var/run/docker.sock" not in " ".join(cmd)
    meta = json.loads((run_dir / "sessions" / ".session_meta.json").read_text())
    assert meta["needs_nested_docker"] is True
    assert meta["session_runtime"] == "privileged"


def test_command_shape_is_identical_for_both_image_families(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """The regression pin for the silent setup hang: whether a startup
    script runs must never depend on needs_nested_docker, so the docker
    command after the image name is byte-identical for both values — one
    shared entrypoint receives the same full harness argv either way."""
    _run(needs_nested_docker=True, output_dir=str(tmp_path / "a"))
    _run(needs_nested_docker=False, output_dir=str(tmp_path / "b"))
    dind, plain = captured_run
    i, j = dind.index("session-widgets"), plain.index("session-widgets")
    assert dind[i:] == plain[j:]
    assert "--entrypoint" not in dind and "--entrypoint" not in plain


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
    image_index = cmd.index("session-widgets")
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
    # Isolation is orthogonal to the command: one entrypoint, one shape.
    assert "--entrypoint" not in cmd
    image_index = cmd.index("session-widgets")
    assert cmd[image_index + 1] == "sh"


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
    # One shared image entrypoint receives the full harness argv.
    assert "--entrypoint" not in cmd
    image_index = cmd.index("autonomy-session-platform")
    assert cmd[image_index + 1] == "codex"
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
    assert "--entrypoint" not in cmd
    image_index = cmd.index("autonomy-session-platform")
    assert cmd[image_index + 1] == "sh"
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


# ── Grok Build (xAI) harness ─────────────────────────────────────────

def _grok_shell(cmd: list[str]) -> str:
    """The `sh -c` body every Grok launch execs through the shared entrypoint."""
    assert cmd[cmd.index("sh") + 1] == "-c"
    return cmd[cmd.index("sh") + 2]


def test_grok_interactive_launches_the_tui_with_trust_and_always_approve(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    monkeypatch.setattr(session_launcher, "_grok_vault_key_available", lambda: False)
    out = _run(
        output_dir=str(tmp_path / "run"),
        harness="grok",
        image="autonomy-session-platform",
        model="grok-4.6",
    )
    assert out == "fake-container-id"
    cmd = captured_run[0]
    assert "--entrypoint" not in cmd
    assert cmd[cmd.index("autonomy-session-platform") + 1] == "sh"
    shell = _grok_shell(cmd)
    assert "exec grok --trust --always-approve -m grok-4.6 --session-id " in shell
    assert shell.endswith("--no-alt-screen")
    assert "grok login" not in shell                      # first-party: no sign-in step
    assert "cp /workspace/output/grok-config.toml" in shell
    assert "GROK_HOME=/home/agent/.grok" in " ".join(cmd)
    # The transcript tree binds where Grok writes sessions/.
    assert f"{tmp_path / 'run' / 'sessions'}:/home/agent/.grok/sessions" in " ".join(cmd)
    # No Claude credential mount, no Codex home mounts.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in " ".join(cmd)
    assert "--dangerously-skip-permissions" not in shell
    config = (tmp_path / "run" / "grok-config.toml").read_text()
    assert 'permission_mode = "always-approve"' in config
    assert "[auth]" not in config
    assert 'default = "grok-4.6"' in config
    meta = json.loads((tmp_path / "run" / "sessions" / ".session_meta.json").read_text())
    assert meta["harness"] == "grok"
    assert meta["grok_mode"] == "xai"
    assert meta["grok_session_id"] in shell


def test_grok_first_party_key_defaults_from_the_vault(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    monkeypatch.setattr(session_launcher, "_grok_vault_key_available", lambda: True)
    resolved = {}
    def _resolve(key):
        resolved["key"] = key
        return "xai-secret"
    monkeypatch.setattr(session_launcher, "_resolve_credential", _resolve)
    _run(output_dir=str(tmp_path / "run"), harness="grok")
    joined = " ".join(captured_run[0])
    assert resolved["key"] == "grok.api-key"
    assert "XAI_API_KEY=xai-secret" in joined
    assert "credential:grok.api-key" not in joined


def test_grok_workspace_key_wins_over_the_vault_default(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    monkeypatch.setattr(session_launcher, "_grok_vault_key_available", lambda: True)
    monkeypatch.setattr(session_launcher, "_resolve_credential", lambda key: f"resolved:{key}")
    _run(output_dir=str(tmp_path / "run"), harness="grok",
         extra_env={"XAI_API_KEY": "credential:xai.team-key"})
    joined = " ".join(captured_run[0])
    assert "XAI_API_KEY=resolved:xai.team-key" in joined
    assert "grok.api-key" not in joined


def test_grok_gateway_mode_writes_catalog_and_signs_in_first(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    monkeypatch.setattr(session_launcher, "_resolve_credential", lambda key: "sk-or-gateway")
    _run(
        output_dir=str(tmp_path / "run"),
        harness="grok",
        model="x-ai/grok-4.6",
        extra_env={
            "GROK_GATEWAY_BASE_URL": "https://openrouter.ai/api/v1/",
            "GROK_GATEWAY_API_KEY": "credential:autonomy:openrouter.api-key",
            "GROK_GATEWAY_MODELS": "x-ai/grok-4.7,x-ai/grok-build-0.1",
        },
    )
    cmd = captured_run[0]
    joined = " ".join(cmd)
    shell = _grok_shell(cmd)
    assert "grok login >/dev/null 2>&1 || true; exec grok --trust --always-approve -m x-ai-grok-4-6 " in shell
    assert "GROK_GATEWAY_API_KEY=sk-or-gateway" in joined
    assert "XAI_API_KEY" not in joined                    # never a first-party key for a gateway
    config = (tmp_path / "run" / "grok-config.toml").read_text()
    assert 'auth_provider_command = "/usr/local/bin/autonomy-grok-auth"' in config
    assert 'default = "x-ai-grok-4-6"' in config
    for key, model_id in (("x-ai-grok-4-6", "x-ai/grok-4.6"), ("x-ai-grok-4-7", "x-ai/grok-4.7"),
                          ("x-ai-grok-build-0-1", "x-ai/grok-build-0.1")):
        assert f"[model.{key}]" in config
        assert f'model = "{model_id}"' in config
    assert 'base_url = "https://openrouter.ai/api/v1"' in config   # trailing slash trimmed
    assert 'env_key = "GROK_GATEWAY_API_KEY"' in config
    assert "sk-or-gateway" not in config                  # the config never holds the key
    meta = json.loads((tmp_path / "run" / "sessions" / ".session_meta.json").read_text())
    assert meta["grok_mode"] == "gateway"


def test_grok_noninteractive_runs_headless_with_prompt_file(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    monkeypatch.setattr(session_launcher, "_grok_vault_key_available", lambda: False)
    _run(
        output_dir=str(tmp_path / "run"),
        harness="grok",
        image="autonomy-session-platform",
        prompt="Write a summary.",
        model=None,
    )
    cmd = captured_run[0]
    shell = _grok_shell(cmd)
    assert "--output-format streaming-json --prompt-file /workspace/output/.prompt.md" in shell
    assert "--no-alt-screen" not in shell
    assert " -m " not in shell                            # no model → Grok's own default
    assert (tmp_path / "run" / ".prompt.md").read_text() == "Write a summary."


def test_grok_resume_passes_the_session_uuid_and_no_new_id(
    tmp_path, fake_crosstalk, captured_run, monkeypatch,
):
    monkeypatch.setattr(session_launcher, "_resolve_credentials", lambda: None)
    monkeypatch.setattr(session_launcher, "_grok_vault_key_available", lambda: False)
    run_dir = tmp_path / "run"
    (run_dir / "sessions").mkdir(parents=True)
    _run(
        output_dir=str(run_dir),
        harness="grok",
        resume_uuid="01a0c7a2-98ab-70d1-9905-1dcb9489fc45",
        model="grok-4.6",
    )
    shell = _grok_shell(captured_run[0])
    assert "--resume 01a0c7a2-98ab-70d1-9905-1dcb9489fc45" in shell
    assert "--session-id" not in shell


def test_grok_capability_skills_project_into_the_claude_skills_dir(tmp_path, monkeypatch):
    """Grok scans ~/.claude/skills (Claude compatibility on by default), so
    the personal-dir projection Claude gets applies to Grok too; Codex keeps
    its deliberate gap."""
    skill = tmp_path / "repo" / "agents" / "capabilities" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: A demo skill\n---\n# Demo\n")
    monkeypatch.setattr(session_launcher, "REPO_ROOT", tmp_path / "repo")
    cap = MaterializedCapability(
        contract="demo@1", contract_version=1, implementation="autonomy/demo",
        implementation_version=1, delivery_mode="package",
        package_root=str(tmp_path / "pkg"),
        mount_target="/opt/autonomy/capabilities/autonomy-demo",
        skill_path="agents/capabilities/demo/SKILL.md",
    )
    run_dir = tmp_path / "run"
    grok_mounts = session_launcher._capability_skill_surface((cap,), run_dir, "grok")
    assert list(grok_mounts.values()) == ["/home/agent/.claude/skills/demo:ro"]
    assert session_launcher._capability_skill_surface((cap,), run_dir, "codex") == {}


def test_render_grok_config_uses_toml_safe_model_keys():
    from agents.session_launcher import grok_model_key, render_grok_config, GrokLaunchProfile
    assert grok_model_key("x-ai/grok-4.6") == "x-ai-grok-4-6"
    assert grok_model_key("grok-4.6") == "grok-4-6"
    assert grok_model_key("") == "gateway-model"
    profile = GrokLaunchProfile(mode="gateway", base_url="https://gw.example/v1",
                                models=("a/b.c",), default_model="a-b-c", context_window=128000)
    text = render_grok_config(profile)
    assert "[model.a-b-c]" in text and "context_window = 128000" in text


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


def _account_from_rows(org_uuid, token_row, cred_row):
    from tools.graph import harness_credentials as hv
    parts = {}
    if token_row is not None:
        parts["setup"] = token_row.payload.get("raw_key")
        if getattr(token_row, "created_at", None):
            parts["setup_minted_at"] = token_row.created_at
    if cred_row is not None:
        p = cred_row.payload or {}
        for part, name in (("alias", "alias"), ("access", "access_token"),
                           ("refresh", "refresh_token"), ("email", "account_email"),
                           ("org_name", "organization_name")):
            if p.get(name):
                parts[part] = p[name]
        if isinstance(p.get("expires_at_ms"), int):
            parts["expires"] = str(p["expires_at_ms"])
    return hv.Account("claude", org_uuid, parts)


@pytest.fixture
def picker_seams(monkeypatch):
    """The picker reads accounts from the vault; these tests describe them
    as the setup-token rows and credential rows the record migrated."""
    monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: [], raising=False)
    monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: [], raising=False)

    def accounts():
        tokens = {r.key: r for r in session_launcher._setup_token_rows()}
        creds = {r.key: r for r in session_launcher._credentials_rows()}
        out = [_account_from_rows(k, tokens.get(k), creds.get(k)) for k in sorted(set(tokens) | set(creds))]
        return [a for a in out if a.launchable]
    monkeypatch.setattr(session_launcher, "_claude_accounts", accounts)


@pytest.mark.usefixtures("picker_seams")
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

    def test_c_no_account_refuses_and_runs_nothing(self, monkeypatch, freeze_now):
        """No launchable account → None with the remedy logged; the
        interactive install is never run from a launch."""
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: [])
        labels = _credentials_row("org-A", "gmail")
        labels.payload.pop("access_token"); labels.payload.pop("refresh_token")
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: [labels])
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: [])

        def _no_run(*a, **kw):
            raise AssertionError("no subprocess may run from the picker")
        monkeypatch.setattr(session_launcher.subprocess, "run", _no_run)
        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None,
        )
        assert result is None

    def test_c_cold_vault_refuses_without_install(self, monkeypatch, freeze_now):
        from tools.graph import harness_credentials as hv
        cold = hv.Account("claude", "org-A", {}, openable=False)
        monkeypatch.setattr(session_launcher, "_claude_accounts", lambda: [])
        monkeypatch.setattr(hv, "list_accounts", lambda harness, **kw: [cold])

        def _no_run(*a, **kw):
            raise AssertionError("no subprocess may run from the picker")
        monkeypatch.setattr(session_launcher.subprocess, "run", _no_run)
        assert session_launcher._resolve_credentials_via_substrate(prefer_alias=None) is None

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
        """An account whose setup token is past its year, with no OAuth
        bundle to launch from, is not launchable and never reaches the picker
        (tools/graph/harness_credentials.py SETUP_TOKEN_TTL)."""
        from datetime import timedelta
        from tools.graph import harness_credentials as hv
        fresh = _FakeRow(
            key="org-A", payload={"raw_key": "raw-A"},
            created_at=freeze_now.isoformat(),
        )
        expired_at = freeze_now - hv.SETUP_TOKEN_TTL - timedelta(days=1)
        old = _FakeRow(
            key="org-B", payload={"raw_key": "raw-B"},
            created_at=expired_at.isoformat(),
        )
        monkeypatch.setattr(session_launcher, "_setup_token_rows", lambda: [fresh, old])
        labels = _credentials_row("org-A", "gmail")
        labels.payload.pop("access_token"); labels.payload.pop("refresh_token")
        monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: [labels])
        monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: [])

        class _PinnedRng:
            def choice(self, seq):
                # The picker should only see the fresh account, so seq[0] is org-A.
                assert len(seq) == 1
                return seq[0]
        result = session_launcher._resolve_credentials_via_substrate(
            prefer_alias=None, rng=_PinnedRng(),
        )
        assert result["harness_token"] == "org-A"


@pytest.mark.usefixtures("picker_seams")
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


def test_sessions_get_no_secret_mount_at_launch(
    tmp_path, fake_creds, fake_crosstalk, captured_run,
):
    """Secret delivery is a PRIVATE in-container ramfs created at release
    time — the launch command must carry NO secret-related mount at all.
    A shared delivery root gave sibling dashboards the power to destroy
    every session's secrets (2026-08-30); its bind must not return."""
    _run(name="auto-a", output_dir=str(tmp_path / "run-a"))
    cmd = captured_run[0]
    joined = " ".join(cmd)
    assert "autonomy-secrets" not in joined
    assert "dst=/run/secrets" not in joined

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

def _stub_vault(monkeypatch, harness_accounts: dict):
    """Stub the vault: ``{harness: [Account, ...]}``."""
    from tools.graph import harness_credentials as hv
    monkeypatch.setattr(
        hv, "list_accounts",
        lambda harness, **kw: list(harness_accounts.get(harness, [])),
    )
    monkeypatch.setattr(
        hv, "read_account",
        lambda harness, account_id, **kw: next(
            (a for a in harness_accounts.get(harness, []) if a.id == account_id), None),
    )


def _codex_vault(monkeypatch, account_id="acct-UUID", **over):
    from tools.graph import harness_credentials as hv
    parts = {"id": "id-1", "access": "at-1", "refresh": "rt-1", "expires": "4102444800000",
             "email": "codexuser@example.com", "refreshed_at": "2026-08-14T00:00:00Z"}
    parts.update(over)
    _stub_vault(monkeypatch, {"codex": [hv.Account("codex", account_id, parts)]})


def test_codex_signin_is_the_auth_json_codex_expects(monkeypatch):
    """The account's vault rows are rebuilt, in memory, into the auth.json
    shape Codex expects."""
    _codex_vault(monkeypatch)
    payloads = session_launcher._signin_payloads(None)
    doc = json.loads(payloads[session_launcher.CODEX_AUTH_FILENAME])
    assert doc["auth_mode"] == "chatgpt"
    assert doc["OPENAI_API_KEY"] is None
    assert doc["tokens"]["account_id"] == "acct-UUID"
    assert doc["tokens"]["access_token"] == "at-1"
    assert doc["tokens"]["refresh_token"] == "rt-1"
    assert doc["tokens"]["id_token"] == "id-1"
    assert doc["last_refresh"] == "2026-08-14T00:00:00Z"


def test_no_codex_account_means_no_codex_signin_and_a_warning(monkeypatch, caplog):
    """No Codex account → Codex simply unavailable (truthful), WARNED with the
    remedy — a missing sign-in is operator-visible, not a silent prompt."""
    _stub_vault(monkeypatch, {})
    with caplog.at_level("INFO", logger=session_launcher.logger.name):
        assert session_launcher._signin_payloads(None) == {}
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warns) == 1
    msg = warns[0].getMessage()
    assert "no Codex account in the vault" in msg
    assert "graph credentials import" in msg


def test_pick_account_is_the_only_one_or_a_random_one(monkeypatch):
    from tools.graph import harness_credentials as hv
    one = hv.Account("codex", "a", {"id": "i", "access": "a", "refresh": "r"})
    two = hv.Account("codex", "b", {"id": "i", "access": "a", "refresh": "r"})
    incomplete = hv.Account("codex", "c", {"id": "i"})
    _stub_vault(monkeypatch, {"codex": [one, incomplete]})
    assert session_launcher._pick_account("codex") is one
    _stub_vault(monkeypatch, {"codex": [one, two]})
    class _Rng:
        def choice(self, seq):
            return seq[-1]
    assert session_launcher._pick_account("codex", rng=_Rng()) is two
    _stub_vault(monkeypatch, {"codex": [incomplete]})
    assert session_launcher._pick_account("codex") is None


def test_optional_tool_mounts_carry_no_signin(tmp_path, monkeypatch):
    """auto-1cc4q: with every harness account in the vault, no sign-in is
    mounted — they reach the container only through its private ramfs."""
    from tools.graph import harness_credentials as hv
    _stub_vault(monkeypatch, {
        "codex": [hv.Account("codex", "a", {"id": "i", "access": "a", "refresh": "r"})],
        "grok": [hv.Account("grok", "default", {"auth": '{"t": 1}'})],
    })
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mounts = session_launcher._resolve_optional_tool_mounts(run_dir=run_dir)
    dests = {spec.split(":")[0] for spec in mounts.values()}
    assert not dests & set(session_launcher.SIGNIN_CONTAINER_PATHS.values())
    assert list(run_dir.iterdir()) == []


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


def test_detached_launch_delivers_signins_and_mounts_none(
    tmp_path, fake_crosstalk, captured_run, monkeypatch, signin_deliveries,
):
    """auto-1cc4q: the sign-ins go into the running container's private
    ramfs, the harness argv waits for and links them, and nothing is
    mounted from or written to the run dir."""
    from tools.graph import harness_credentials as hv
    _stub_vault(monkeypatch, {
        "codex": [hv.Account("codex", "a", {"id": "i", "access": "a", "refresh": "r"})],
        "grok": [hv.Account("grok", "default", {"auth": '{"t": 1}'})],
    })
    run_dir = tmp_path / "run"
    _run(name="auto-s", output_dir=str(run_dir), harness="codex")
    cmd = captured_run[0]
    # A codex session gets only its own sign-in (auto-9hu6y).
    assert signin_deliveries["now"] == [
        ("auto-s", [session_launcher.CODEX_AUTH_FILENAME])]
    assert signin_deliveries["background"] == []
    joined = " ".join(cmd)
    for dest in session_launcher.SIGNIN_CONTAINER_PATHS.values():
        assert f"{dest}:ro" not in joined and f"dst={dest}" not in joined
    i = cmd.index("session-widgets")
    assert cmd[i + 1:i + 3] == ["sh", "-c"] and cmd[i + 4] == "autonomy-signin"
    assert cmd[i + 5] == "codex"
    assert not list(run_dir.rglob("*auth*.json"))


def test_tmux_launch_delivers_signins_in_the_background(
    tmp_path, fake_crosstalk, monkeypatch, signin_deliveries,
):
    _codex_vault(monkeypatch)
    out = _run(name="auto-t", detach=False, output_dir=str(tmp_path / "run"),
               harness="codex")
    assert isinstance(out, str) and "autonomy-signin" in out
    assert signin_deliveries["background"] == [
        ("auto-t", [session_launcher.CODEX_AUTH_FILENAME])]
    assert signin_deliveries["now"] == []


def test_detached_launch_with_undelivered_signin_is_removed(
    tmp_path, fake_crosstalk, monkeypatch,
):
    _codex_vault(monkeypatch)
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "cid\n", ""

    monkeypatch.setattr(session_launcher.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or Done())
    monkeypatch.setattr(session_launcher, "deliver_signins",
                        lambda name, payloads: sorted(payloads))
    assert _run(name="auto-u", output_dir=str(tmp_path / "run"),
                harness="codex") is None
    assert ["docker", "rm", "-f", "auto-u"] in calls


def test_signin_prefix_waits_links_and_execs(tmp_path, monkeypatch):
    """The in-container prefix, run for real with /run/secrets redirected:
    a delivered file is linked where the harness reads it, a missing one is
    reported, and the harness argv is exec'd either way."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "codex-auth.json").write_text("{}")
    home = tmp_path / "home"
    monkeypatch.setattr(session_launcher, "SIGNIN_WAIT_S", 0.3)
    monkeypatch.setattr(session_launcher, "SIGNIN_CONTAINER_PATHS", {
        "codex-auth.json": str(home / ".codex" / "auth.json"),
        "grok-auth.json": str(home / ".grok" / "auth.json"),
    })
    argv = session_launcher.signin_argv_prefix(["codex-auth.json", "grok-auth.json"])
    argv[2] = argv[2].replace("/run/secrets", str(secrets_dir))
    r = subprocess.run([*argv, "echo", "harness-ran"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "harness-ran"
    link = home / ".codex" / "auth.json"
    assert link.is_symlink() and os.readlink(link) == str(secrets_dir / "codex-auth.json")
    assert not (home / ".grok" / "auth.json").exists()
    assert "grok-auth.json was not delivered" in r.stderr


def test_launcher_source_has_no_signin_staging():
    """auto-1cc4q: no run_dir staging and no post-exit cleanup thread."""
    import inspect
    src = inspect.getsource(session_launcher)
    for gone in ("_schedule_creds_cleanup", "_write_private_json",
                 "_materialize_codex_auth_json", "creds_copy"):
        assert gone not in src


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
    # Compared against the package's own constant, not a literal. The literal
    # was "0.5.1" and sat here broken from 08996c23 (2026-09-10) until
    # at-0922-180743-8e73: what this line is for is that the shim resolves and
    # the CLI runs, and pinning a version string turns every legitimate bump
    # into a failure in a file that has nothing to do with agent-test's
    # version.
    from tools.agent_test import __version__ as agent_test_version

    assert stdout.strip() == agent_test_version

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
    run_dir = tmp_path / "run"
    gmd = tmp_path / "CLAUDE.md"; gmd.write_text("primer")
    startup = tmp_path / "startup.sh"; startup.write_text("#!/bin/sh\n")
    ws_src = tmp_path / "wsmount"; ws_src.mkdir()

    session_launcher.launch_session(
        session_type="dispatch", name="test-session", prompt=None, detach=True,
        image="session-widgets", metadata={"org": "test-org"},
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
        "-e", "GROK_HOME=/home/agent/.grok",
        "-e", "CLAUDE_CODE_OAUTH_TOKEN=tok-xyz",
        "-v", "{DATA}/.beads:/data/.beads",
        "-v", "/dev/null:/data/.beads/.beads-credential-key:ro",
        "-v", "{RUN}:/workspace/output",
        "-v", "{RUN}/sessions:/home/agent/.claude/projects",
        "-v", "{TMP}/wsmount:/opt/data:ro",
        "-v", "{SNAP}:/workspace/repo:ro",
        "-v", "{DATA}/uploads:/workspace/repo/data/uploads:ro",
        "-v", "{RUN}/cap-bin:/etc/autonomy/cap-bin:ro",
        "-v", "{TMP}/codex-config.toml:/home/agent/.codex/config.toml:ro",
        "-v", "{TMP}/CLAUDE.md:/home/agent/.claude/CLAUDE.md:ro",
        "-v", "{TMP}/CLAUDE.md:/home/agent/.codex/AGENTS.md:ro",
        "-v", "{TMP}/startup.sh:/startup.sh:ro",
        "-e", "AUTONOMY_CAPABILITY_BIN=/etc/autonomy/cap-bin",
        "-w", "/workspace/repo",
        "session-widgets", "claude", "--dangerously-skip-permissions",
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
    plan, _se = session_launcher.build_mount_plan(
        run_dir=run_dir, sessions_dir=run_dir / "sessions",
        harness="claude", working_dir="/workspace/repo",
        startup_script="/var/run/docker.sock",
    )
    with pytest.raises(SocketMountRefused):
        mount_args(plan, NodeTopology(is_host_process=True))


def test_mount_refusal_mints_no_token_and_opens_no_signin(tmp_path, fake_creds, monkeypatch):
    """auto-vm8qh criterion 6: a mount refusal returns having minted NO session
    token and opened NO sign-in from the vault."""
    import types
    minted, opened = [], []
    fake_dao = types.SimpleNamespace(
        auth_db=types.SimpleNamespace(insert_token=lambda *a, **k: minted.append(a)))
    monkeypatch.setitem(__import__("sys").modules, "tools.dashboard.dao", fake_dao)
    monkeypatch.setattr(session_launcher, "_signin_payloads",
                        lambda acct, **_k: opened.append(acct) or {})
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    result = session_launcher.launch_session(
        session_type="dispatch", name="t", prompt=None, detach=True,
        image="x", metadata={"org": "o"}, output_dir=str(tmp_path / "run"),
        mounts={"/var/run/docker.sock": "/data/leak"},   # forces SocketMountRefused
    )
    assert result is None
    assert minted == [], "no session token may be minted on a refused launch"
    assert opened == [], "no sign-in may be opened on a refused launch"


def test_unopenable_claude_account_refuses_before_token(tmp_path, monkeypatch):
    """A vault Claude account chosen for the launch that cannot be opened is a
    refusal taken BEFORE the session token is minted — never a container that
    starts signed out."""
    import types
    minted = []
    fake_dao = types.SimpleNamespace(
        auth_db=types.SimpleNamespace(insert_token=lambda *a, **k: minted.append(a)))
    monkeypatch.setitem(__import__("sys").modules, "tools.dashboard.dao", fake_dao)
    monkeypatch.setattr(session_launcher, "_resolve_credentials",
                        lambda: {"type": "vault", "harness_token": "org-1"})
    _claude_vault(monkeypatch, setup="k", bundle=False)
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    result = session_launcher.launch_session(
        session_type="dispatch", name="t", prompt=None, detach=True,
        image="x", metadata={"org": "o"}, output_dir=str(tmp_path / "run"),
    )
    assert result is None
    assert minted == []


def test_build_mount_plan_socket_via_global_claude_md_is_refused(tmp_path, monkeypatch):
    """Integrated socket refusal for the OTHER bypass input (global_claude_md),
    through build_mount_plan (auto-vm8qh criterion 3/4)."""
    import pytest
    from agents.mount_plan import mount_args, NodeTopology, SocketMountRefused
    monkeypatch.setattr(session_launcher, "_ensure_platform_snapshot", lambda: None)
    monkeypatch.setattr(session_launcher, "_resolve_optional_tool_mounts", lambda **k: {})
    run_dir = tmp_path / "run"
    plan, _se = session_launcher.build_mount_plan(
        run_dir=run_dir, sessions_dir=run_dir / "sessions", harness="claude",
        working_dir="/workspace/repo", global_claude_md="/var/run/docker.sock",
    )
    with pytest.raises(SocketMountRefused):
        mount_args(plan, NodeTopology(is_host_process=True))


def test_build_mount_plan_routes_org_beads_dir(tmp_path, monkeypatch):
    """An org with a provisioned tracker config gets it as /data/.beads;
    everyone else (autonomy included) keeps the shared tracker."""
    monkeypatch.setattr(session_launcher, "DATA_ROOT", tmp_path)
    (tmp_path / ".beads").mkdir()
    org_dir = tmp_path / ".beads" / "orgs" / "anchore"
    org_dir.mkdir(parents=True)
    (org_dir / "metadata.json").write_text("{}")

    def beads_source(org):
        plan, _env = session_launcher.build_mount_plan(
            run_dir=tmp_path / "run", sessions_dir=tmp_path / "sess",
            harness="claude", working_dir="/workspace/repo", org=org)
        return next(sp.source for sp in plan.specs()
                    if sp.container_spec.split(":")[0] == "/data/.beads"
                    and sp.source != "/dev/null")

    assert beads_source("anchore") == str(org_dir)
    assert beads_source("autonomy") == str(tmp_path / ".beads")
    assert beads_source(None) == str(tmp_path / ".beads")


def test_beads_credential_env_args_follow_the_org_dir(tmp_path, monkeypatch):
    """Credential env pairs come from the same dir the mount resolves:
    per-org file for a provisioned org, shared file otherwise, no args
    when no credentials file exists."""
    import tools.data_paths as data_paths
    monkeypatch.setattr(data_paths, "DATA_ROOT", tmp_path)
    shared = tmp_path / ".beads"
    shared.mkdir()
    (shared / "credentials.env").write_text(
        "BEADS_DOLT_SERVER_USER=beads_autonomy\nBEADS_DOLT_PASSWORD=pw-a\n")
    org_dir = tmp_path / ".beads" / "orgs" / "anchore"
    org_dir.mkdir(parents=True)
    (org_dir / "metadata.json").write_text("{}")
    (org_dir / "credentials.env").write_text(
        "BEADS_DOLT_SERVER_USER=beads_anchore\nBEADS_DOLT_PASSWORD=pw-n\n")

    args = session_launcher._beads_credential_env_args("anchore")
    assert "BEADS_DOLT_SERVER_USER=beads_anchore" in args
    assert "BEADS_DOLT_PASSWORD=pw-n" in args
    args = session_launcher._beads_credential_env_args("autonomy")
    assert "BEADS_DOLT_SERVER_USER=beads_autonomy" in args
    args = session_launcher._beads_credential_env_args(None)
    assert "BEADS_DOLT_SERVER_USER=beads_autonomy" in args
    (shared / "credentials.env").unlink()
    assert session_launcher._beads_credential_env_args(None) == []


# ── Claude and Grok launch from the vault accounts (record v16 §10.9) ──


def _claude_vault(monkeypatch, *, setup=None, minted_at=None, bundle=True, account_id="org-1"):
    from tools.graph import harness_credentials as hv
    parts = {"alias": "dev"}
    if setup:
        parts["setup"] = setup
        parts["setup_minted_at"] = minted_at or "2026-09-01T00:00:00Z"
    if bundle:
        parts.update({"access": "at-v", "refresh": "rt-v", "expires": "9000",
                      "scopes": "user:inference"})
    _stub_vault(monkeypatch, {"claude": [hv.Account("claude", account_id, parts)]})


def test_claude_signin_is_the_bundle_claude_reads(monkeypatch):
    _claude_vault(monkeypatch)
    raw = session_launcher._signin_payloads("org-1")[session_launcher.CLAUDE_BUNDLE_FILENAME]
    doc = json.loads(raw)["claudeAiOauth"]
    assert doc["accessToken"] == "at-v" and doc["refreshToken"] == "rt-v"
    assert doc["expiresAt"] == 9000 and doc["scopes"] == ["user:inference"]


def test_claude_account_without_bundle_refuses(monkeypatch):
    _claude_vault(monkeypatch, setup="k", bundle=False)
    assert session_launcher._signin_payloads("org-1") is None


def test_picker_takes_the_bundle_when_the_setup_token_is_stale(monkeypatch):
    _claude_vault(monkeypatch, setup="k", minted_at="2020-01-01T00:00:00Z")
    monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: [])
    creds = session_launcher._resolve_credentials_via_substrate(prefer_alias=None)
    assert creds == {"harness_token": "org-1", "alias": "dev", "type": "vault"}
    assert session_launcher._setup_auth_docker_args(creds, Path("/tmp")) == []


def test_picker_takes_a_fresh_setup_token_first(monkeypatch):
    _claude_vault(monkeypatch, setup="sk-1")
    monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: [])
    creds = session_launcher._resolve_credentials_via_substrate(prefer_alias=None)
    assert creds["type"] == "token" and creds["token"] == "sk-1"


def test_grok_signin_is_the_stored_sign_in(monkeypatch):
    from tools.graph import harness_credentials as hv
    _stub_vault(monkeypatch, {"grok": [hv.Account("grok", "default", {"auth": '{"access_token": "g"}'})]})
    payloads = session_launcher._signin_payloads(None)
    assert payloads[session_launcher.GROK_AUTH_FILENAME] == b'{"access_token": "g"}'


def test_launcher_source_reads_no_plaintext_credential_set():
    source = Path(session_launcher.__file__).read_text()
    for name in ("CLAUDE_SETUP_TOKENS_SET_ID", "CLAUDE_CREDENTIALS_SET_ID",
                 "CODEX_CREDENTIALS_SET_ID", "_setup_token_rows", "_credentials_rows"):
        assert name not in source


def test_accounts_migrate_pre_vault_rows_once_when_the_vault_is_empty(monkeypatch):
    """A hot-reloaded dashboard has not run the startup migration; the first
    launch migrates the plaintext rows itself rather than failing."""
    from tools.graph import harness_credentials as hv
    calls: list[str] = []
    state = {"accounts": []}
    monkeypatch.setattr(hv, "list_accounts", lambda harness, **kw: list(state["accounts"]))

    def migrate():
        calls.append("migrate")
        state["accounts"] = [hv.Account("claude", "org-1", {"setup": "k"})]
        return {"claude": 1, "setup_tokens": 1, "codex": 0, "deprecated": 2}
    monkeypatch.setattr(hv, "migrate_plaintext_accounts", migrate)
    assert [a.id for a in session_launcher._claude_accounts()] == ["org-1"]
    assert calls == ["migrate"]
    # With accounts present the migration is not consulted again.
    assert [a.id for a in session_launcher._claude_accounts()] == ["org-1"]
    assert calls == ["migrate"]


def test_dashboard_read_bearer_prefers_the_dispatcher_token_file(tmp_path, monkeypatch):
    from tools.graph import harness_credentials as hv
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(hv, "REPO_DATA_ROOT", str(tmp_path / "nowhere"))
    monkeypatch.setenv("CROSSTALK_TOKEN", "inherited-and-revoked")
    assert hv._bearer() == "inherited-and-revoked"
    (tmp_path / hv.DISPATCHER_TOKEN_RELPATH).write_text("scoped-token\n")
    assert hv._bearer() == "scoped-token"


# ── host-terminal profile (auto-wmsp9, graph://89d3c8df-544 §3) ──────

@pytest.fixture
def containerized_node(monkeypatch, tmp_path):
    """A Compose node: the node's code, data and orgs trees are the
    autonomy-code / autonomy-data / autonomy-orgs volumes, a Docker socket
    exists, and the operator home is set."""
    from agents import mount_plan as mp

    repo = str(session_launcher.REPO_ROOT)
    topo = mp.NodeTopology(
        is_host_process=False,
        volumes=(
            mp.NodeVolume("autonomy-code", repo),
            mp.NodeVolume("autonomy-data", str(session_launcher.DATA_ROOT)),
            mp.NodeVolume("autonomy-orgs", os.path.join(repo, "orgs")),
        ),
        network="autonomy_default",
    )
    monkeypatch.setattr(mp, "discover_topology", lambda: topo)
    # Tool mounts come from the node user's home, which this fake topology
    # does not carry; they are not what these tests are about.
    monkeypatch.setattr(
        session_launcher, "_resolve_optional_tool_mounts", lambda **kw: {},
    )
    socket = tmp_path / "docker.sock"
    socket.write_text("")
    monkeypatch.setattr(session_launcher, "HOST_DOCKER_SOCKET", str(socket))
    monkeypatch.setenv("AUTONOMY_HOST_HOME", "/home/operator")
    return socket


def _run_host_terminal(tmp_path, **kw):
    return _run(
        session_type="terminal",
        name="host-0926-000000",
        detach=False,
        host_terminal=True,
        metadata={"tmux_session": "host-0926-000000", "org": "personal"},
        output_dir=str(tmp_path / "run"),
        **kw,
    )


def test_host_terminal_mounts_the_node_socket_and_home(
    tmp_path, fake_creds, fake_crosstalk, containerized_node, platform_snapshot,
):
    cmd = _run_host_terminal(tmp_path).split()
    joined = " ".join(cmd)
    socket = str(containerized_node)

    assert "--mount type=volume,src=autonomy-code,dst=/workspace/repo" in joined
    assert "--mount type=volume,src=autonomy-data,dst=/workspace/repo/data" in joined
    assert "--mount type=volume,src=autonomy-orgs,dst=/workspace/repo/orgs" in joined
    for mount in ("autonomy-code,dst=/workspace/repo", "autonomy-data,dst=/workspace/repo/data",
                  "autonomy-orgs,dst=/workspace/repo/orgs"):
        idx = joined.index(mount)
        assert not joined[idx:].split(" ", 1)[0].endswith("readonly")
    assert joined.count(f"{socket}:{socket}") == 1
    # Private propagation: rslave needs a shared/slave source mount, which a
    # home under a private / (WSL2) is not; docker refused the launch.
    assert "--mount type=bind,src=/home/operator,dst=/host-home,readonly" in joined
    assert "dst=/host-home,bind-propagation" not in joined
    # Full /mnt, writable, plain (private) propagation: rslave is refused
    # where the host's / is private. Writable, so NOT readonly.
    assert "--mount type=bind,src=/mnt,dst=/mnt" in joined
    mnt_arg = next(a for a in cmd if a.startswith("type=bind,src=/mnt,dst=/mnt"))
    assert "bind-propagation" not in mnt_arg and "readonly" not in mnt_arg
    assert cmd[cmd.index("--group-add") + 1] == str(containerized_node.stat().st_gid)
    assert "AUTONOMY_DATA_ROOT=/workspace/repo/data" in cmd
    assert cmd[cmd.index("-w") + 1] == "/workspace/repo"
    assert session_launcher.HOST_TERMINAL_IMAGE in cmd
    # The node's live code replaces the read-only snapshot and its uploads view.
    assert platform_snapshot.calls == []
    assert "/workspace/repo:ro" not in joined
    assert "/workspace/repo/data/uploads" not in joined


def test_host_terminal_token_is_a_local_operator_token(
    tmp_path, fake_creds, containerized_node, monkeypatch,
):
    import sys
    import types
    minted = []
    fake = types.SimpleNamespace(insert_token=lambda *a: minted.append(a))
    monkeypatch.setitem(sys.modules, "tools.dashboard.dao",
                        types.SimpleNamespace(auth_db=fake))
    _run_host_terminal(tmp_path)
    assert [(name, org) for _, name, org in minted] == [("host-0926-000000", None)]


def test_host_terminal_without_socket_raises_before_minting(
    tmp_path, fake_creds, containerized_node, monkeypatch, captured_run,
):
    import sys
    import types
    minted = []
    fake = types.SimpleNamespace(insert_token=lambda *a: minted.append(a))
    monkeypatch.setitem(sys.modules, "tools.dashboard.dao",
                        types.SimpleNamespace(auth_db=fake))
    containerized_node.unlink()
    with pytest.raises(RuntimeError, match="Docker socket"):
        _run_host_terminal(tmp_path)
    assert minted == []
    assert not (tmp_path / "run").exists()


def test_host_terminal_without_operator_home_raises(
    tmp_path, fake_creds, fake_crosstalk, containerized_node, monkeypatch,
):
    monkeypatch.delenv("AUTONOMY_HOST_HOME")
    with pytest.raises(RuntimeError, match="AUTONOMY_HOST_HOME"):
        _run_host_terminal(tmp_path)


def test_workspace_socket_mount_is_still_refused_on_a_containerized_node(
    tmp_path, fake_creds, fake_crosstalk, containerized_node, captured_run,
):
    result = _run(
        session_type="terminal",
        mounts={"/var/run/docker.sock": "/var/run/docker.sock"},
        output_dir=str(tmp_path / "run"),
    )
    assert result is None
    assert captured_run == []


# ── per-session nested-Docker volume (auto-ipq3l) ────────────────────

@pytest.fixture
def docker_volumes(monkeypatch):
    """Record every docker call; `volume inspect` reports a missing volume
    until one was created."""
    calls: list[list[str]] = []
    created: set[str] = set()

    class Done:
        def __init__(self, rc=0, out="fake-container-id\n"):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **kwargs):
        if cmd[:1] == ["docker"]:
            calls.append(cmd)
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return Done(0 if cmd[3] in created else 1, "")
        if cmd[:3] == ["docker", "volume", "create"]:
            created.add(cmd[-1])
            return Done(0, cmd[-1])
        return Done()

    monkeypatch.setattr(session_launcher.subprocess, "run", fake_run)
    return calls


def test_privileged_session_mounts_its_labelled_dind_volume(
    tmp_path, fake_creds, fake_crosstalk, docker_volumes,
):
    _run(name="auto-dind", needs_nested_docker=True,
         output_dir=str(tmp_path / "a"))
    create = [c for c in docker_volumes if c[:3] == ["docker", "volume", "create"]]
    assert create == [["docker", "volume", "create",
                       "--label", "autonomy.session=auto-dind",
                       "--label", "autonomy.org=test-org",
                       "autonomy-dind-auto-dind"]]
    run = [c for c in docker_volumes if c[:2] == ["docker", "run"]][0]
    i = run.index("--mount")
    assert run[i + 1] == ("type=volume,src=autonomy-dind-auto-dind,"
                          "dst=/var/lib/docker")
    assert i < run.index("session-widgets")

    # A resume/restart reuses the same volume: no second create.
    _run(name="auto-dind", needs_nested_docker=True,
         output_dir=str(tmp_path / "b"))
    assert len([c for c in docker_volumes
                if c[:3] == ["docker", "volume", "create"]]) == 1
    assert len([c for c in docker_volumes if c[:2] == ["docker", "run"]]) == 2


@pytest.mark.parametrize("runtime", ["standard", "sysbox"])
def test_unprivileged_session_gets_no_dind_volume(
    tmp_path, fake_creds, fake_crosstalk, docker_volumes, runtime,
):
    _run(needs_nested_docker=True, runtime=runtime,
         output_dir=str(tmp_path / "a"))
    assert not any(c[:2] == ["docker", "volume"] for c in docker_volumes)
    run = [c for c in docker_volumes if c[:2] == ["docker", "run"]][0]
    assert "--mount" not in run


def test_dind_volume_create_failure_refuses_the_launch(
    tmp_path, fake_creds, fake_crosstalk, monkeypatch,
):
    calls = []

    class Fail:
        returncode, stdout, stderr = 1, "", "no space left"

    monkeypatch.setattr(session_launcher.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or Fail())
    assert _run(needs_nested_docker=True,
                output_dir=str(tmp_path / "a")) is None
    assert not any(c[:2] == ["docker", "run"] for c in calls)


# ── re-delivery after a reload kills the delivery thread (auto-fgheq) ──


def test_signin_payloads_report_the_chosen_accounts(monkeypatch):
    from tools.graph import harness_credentials as hv
    _stub_vault(monkeypatch, {
        "codex": [hv.Account("codex", "cx-1", {"id": "i", "access": "a", "refresh": "r"})],
        "grok": [hv.Account("grok", "gk-1", {"auth": '{"t": 1}'})],
    })
    accounts = {}
    payloads = session_launcher._signin_payloads(None, accounts_out=accounts)
    assert accounts == {session_launcher.CODEX_AUTH_FILENAME: "cx-1",
                        session_launcher.GROK_AUTH_FILENAME: "gk-1"}
    for filename, account in accounts.items():
        assert session_launcher._open_signin(filename, account) == payloads[filename] \
            or filename == session_launcher.CODEX_AUTH_FILENAME  # codex stamps a time
    assert session_launcher._open_signin(session_launcher.GROK_AUTH_FILENAME, "gone") is None


@pytest.fixture
def pending_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session_launcher, "SIGNIN_PENDING_DIR", tmp_path / "pending")
    return tmp_path / "pending"


def test_background_delivery_keeps_a_pending_record_until_it_finishes(
        pending_dir, monkeypatch):
    import threading
    release = threading.Event()
    delivered = []

    def deliver(name, payloads):
        assert release.wait(10)
        delivered.append(name)
        return []
    monkeypatch.setattr(session_launcher, "deliver_signins", deliver)
    # the autouse fixture replaced it; use the real one here
    monkeypatch.setattr(session_launcher, "deliver_signins_in_background",
                        _REAL_BACKGROUND)
    session_launcher.deliver_signins_in_background(
        "auto-p", {"codex-auth.json": b"{}"}, {"codex-auth.json": "cx-1"})
    record = json.loads((pending_dir / "auto-p.json").read_text())
    assert record["accounts"] == {"codex-auth.json": "cx-1"}
    assert "{}" not in json.dumps(record["accounts"])
    release.set()
    for _ in range(200):
        if not (pending_dir / "auto-p.json").exists():
            break
        __import__("time").sleep(0.01)
    assert delivered == ["auto-p"] and not (pending_dir / "auto-p.json").exists()


_REAL_BACKGROUND = session_launcher.deliver_signins_in_background


def _pending(pending_dir, name, *, accounts, deadline, age):
    import os
    pending_dir.mkdir(parents=True, exist_ok=True)
    path = pending_dir / f"{name}.json"
    path.write_text(json.dumps({"deadline": deadline, "accounts": accounts}))
    t = __import__("time").time() - age
    os.utime(path, (t, t))
    return path


def test_redelivery_reopens_only_what_is_missing(pending_dir, monkeypatch, signin_deliveries):
    import time as _t
    now = _t.time()
    _pending(pending_dir, "auto-r", deadline=now + 60, age=30,
             accounts={"codex-auth.json": "cx-1", "grok-auth.json": "gk-1"})
    monkeypatch.setattr(session_launcher, "_missing_signins",
                        lambda name, files: ["grok-auth.json"])
    monkeypatch.setattr(session_launcher, "_open_signin",
                        lambda f, a: f"{f}:{a}".encode())
    done = session_launcher.redeliver_pending_signins(since=now - 5, now=now)
    assert done == {"auto-r": ["grok-auth.json"]}
    assert signin_deliveries["now"] == [("auto-r", ["grok-auth.json"])]
    assert not (pending_dir / "auto-r.json").exists()


def test_redelivery_skips_its_own_records_and_drops_expired_ones(
        pending_dir, monkeypatch, signin_deliveries):
    import time as _t
    now = _t.time()
    mine = _pending(pending_dir, "auto-mine", deadline=now + 60, age=1,
                    accounts={"codex-auth.json": "cx-1"})
    expired = _pending(pending_dir, "auto-old", deadline=now - 1, age=300,
                       accounts={"codex-auth.json": "cx-1"})
    monkeypatch.setattr(session_launcher, "_missing_signins",
                        lambda name, files: list(files))
    monkeypatch.setattr(session_launcher, "_open_signin", lambda f, a: b"x")
    assert session_launcher.redeliver_pending_signins(since=now - 10, now=now) == {}
    assert mine.exists(), "a record this worker's own thread still owns"
    assert not expired.exists()
    assert signin_deliveries["now"] == []


def test_a_record_owned_by_a_live_process_is_left_to_it(pending_dir, monkeypatch, signin_deliveries):
    """Review of daca2ace: a foreground CLI's delivery thread survives a
    dashboard reload; a second writer would race it on the same ramfs temp
    file and could publish a truncated sign-in."""
    import os
    import time as _t
    now = _t.time()
    path = _pending(pending_dir, "auto-live", deadline=now + 60, age=300,
                    accounts={"codex-auth.json": "cx-1"})
    record = json.loads(path.read_text())
    record.update(owner_pid=os.getpid(),
                  owner_start=session_launcher._process_start_ticks(os.getpid()))
    path.write_text(json.dumps(record))
    os.utime(path, (now - 300, now - 300))   # old enough for the mtime rule
    monkeypatch.setattr(session_launcher, "_missing_signins", lambda n, f: list(f))
    monkeypatch.setattr(session_launcher, "_open_signin", lambda f, a: b"x")
    assert session_launcher.redeliver_pending_signins(since=now, now=now) == {}
    assert path.exists() and signin_deliveries["now"] == []


@pytest.mark.parametrize("owner", ["dead", "reused"])
def test_a_record_whose_owner_is_gone_is_redelivered(pending_dir, monkeypatch,
                                                     signin_deliveries, owner):
    import os
    import time as _t
    now = _t.time()
    path = _pending(pending_dir, "auto-gone", deadline=now + 60, age=1,
                    accounts={"codex-auth.json": "cx-1"})
    record = json.loads(path.read_text())
    ticks = session_launcher._process_start_ticks(os.getpid())
    record.update(owner_pid=os.getpid() if owner == "reused" else 2**22 + 12345,
                  owner_start=ticks + 1 if owner == "reused" else ticks)
    path.write_text(json.dumps(record))
    monkeypatch.setattr(session_launcher, "_missing_signins", lambda n, f: list(f))
    monkeypatch.setattr(session_launcher, "_open_signin", lambda f, a: b"x")
    # since=now-10 would skip it by age; the owner fields take precedence.
    assert session_launcher.redeliver_pending_signins(since=now - 10, now=now) == {
        "auto-gone": ["codex-auth.json"]}


def test_an_expired_record_warns_when_the_harness_started_signed_out(
        pending_dir, monkeypatch, caplog):
    import time as _t
    now = _t.time()
    _pending(pending_dir, "auto-late", deadline=now - 1, age=300,
             accounts={"codex-auth.json": "cx-1"})
    monkeypatch.setattr(session_launcher, "_missing_signins", lambda n, f: list(f))

    class Running:
        stdout, returncode = "true\n", 0
    monkeypatch.setattr(session_launcher.subprocess, "run", lambda *a, **k: Running())
    with caplog.at_level("WARNING", logger=session_launcher.logger.name):
        session_launcher.redeliver_pending_signins(since=now, now=now)
    assert "auto-late started its harness without sign-ins" in caplog.text


def test_the_presence_check_runs_as_the_secret_owner(monkeypatch):
    calls = []

    class Done:
        returncode = 0
    monkeypatch.setattr(session_launcher.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or Done())
    assert session_launcher._missing_signins("auto-x", ["codex-auth.json"]) == []
    assert calls[0][:4] == ["docker", "exec", "-u", "1000"]


# ── auto-9hu6y: a session gets only its own harness's sign-in ─────────


@pytest.mark.parametrize("harness,expected", [
    ("claude", set()),
    ("codex", {"codex-auth.json"}),
    ("grok", {"grok-auth.json"}),
    (None, {"codex-auth.json", "grok-auth.json"}),
])
def test_only_the_sessions_own_harness_signin_is_delivered(monkeypatch, harness, expected):
    from tools.graph import harness_credentials as hv
    _stub_vault(monkeypatch, {
        "codex": [hv.Account("codex", "cx", {"id": "i", "access": "a", "refresh": "r"})],
        "grok": [hv.Account("grok", "gk", {"auth": '{"t": 1}'})],
    })
    assert set(session_launcher._signin_payloads(None, harness=harness)) == expected


def test_a_claude_session_with_a_vault_account_gets_only_its_bundle(monkeypatch):
    _claude_vault(monkeypatch)
    payloads = session_launcher._signin_payloads("org-1", harness="claude")
    assert set(payloads) == {session_launcher.CLAUDE_BUNDLE_FILENAME}
