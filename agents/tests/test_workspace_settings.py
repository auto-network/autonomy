"""Focused tests for workspace Setting composition helpers."""

from __future__ import annotations

import pytest

from agents.workspace_settings import (
    CAPABILITIES_MOUNT_DIR,
    MaterializedCapability,
    RepoMount,
    WorkspaceSettingsError,
    _impl_mount_target,
    _parse_repo,
    _workspace_from_setting,
    invalidate_caches,
    load_workspaces,
    resolve_capabilities,
)
from tools.graph import ops
from tools.graph.schemas.capability_impl import (
    SET_ID as CAPABILITY_IMPL_SET_ID,
    SCHEMA_REVISION as CAPABILITY_IMPL_REVISION,
)
from tools.graph.schemas.org_capability_install import (
    SET_ID as ORG_CAPABILITY_INSTALL_SET_ID,
    SCHEMA_REVISION as ORG_CAPABILITY_INSTALL_REVISION,
)
from tools.graph.schemas.workspace_capability_enable import (
    SET_ID as WORKSPACE_CAPABILITY_ENABLE_SET_ID,
    SCHEMA_REVISION as WORKSPACE_CAPABILITY_ENABLE_REVISION,
)
from tools.graph.schemas.workspace import WORKSPACE_REVISION, WORKSPACE_SET_ID


def test_workspace_from_setting_defaults_harness_to_claude():
    workspace = _workspace_from_setting(
        {"name": "Autonomy", "image": "autonomy-agent:dashboard"},
        workspace_id="autonomy",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.harness == "claude"


def test_workspace_from_setting_reads_codex_harness():
    workspace = _workspace_from_setting(
        {
            "name": "Autonomy Codex",
            "image": "autonomy-agent:dashboard",
            "harness": "codex",
        },
        workspace_id="autonomy-codex",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.harness == "codex"


def test_load_workspaces_includes_raw_personal_owned_workspace(shipped_workspaces):
    """A private workspace may be rooted directly in personal.db.

    Raw state keeps the declaration invisible when personal.db is consulted as
    another org's peer, but the personal org's own loader must still discover
    it and assign personal as its graph routing scope.
    """
    ops.add_setting(
        WORKSPACE_SET_ID,
        WORKSPACE_REVISION,
        key="idea-board",
        payload={
            "name": "Idea Board",
            "image": "autonomy-agent:dashboard",
            "harness": "codex",
            "working_dir": "/workspace/output",
        },
        state="raw",
        org="personal",
    )
    invalidate_caches()

    workspace = load_workspaces()["idea-board"]

    assert workspace.graph_project == "personal"
    assert workspace.harness == "codex"
    assert workspace.repos == ()


def test_legacy_dind_defaults_to_privileged_nested_docker():
    workspace = _workspace_from_setting(
        {
            "name": "Legacy DinD",
            "image": "autonomy-agent:dind",
            "dind": True,
        },
        workspace_id="legacy-dind",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.needs_nested_docker is True
    assert workspace.session_runtime == "privileged"
    assert workspace.dind is True


def test_nested_docker_runtime_is_independently_configurable():
    workspace = _workspace_from_setting(
        {
            "name": "Sysbox DinD",
            "image": "autonomy-agent:dind",
            "needs_nested_docker": True,
            "session_runtime": "sysbox",
        },
        workspace_id="sysbox-dind",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.needs_nested_docker is True
    assert workspace.session_runtime == "sysbox"


def test_direct_nested_docker_model_defaults_to_privileged():
    from agents.workspace_settings import WorkspaceV1
    workspace = WorkspaceV1(
        id="direct",
        name="Direct",
        description="",
        image="autonomy-agent:dind",
        graph_project="autonomy",
        needs_nested_docker=True,
    )
    assert workspace.session_runtime == "privileged"


# ── RepoMount parsing (auto-4sfe9) ───────────────────────────────


def test_parse_repo_defaults_base_source_to_none():
    repo = _parse_repo(
        {"url": "git@github.com:foo/bar.git", "mount": "/workspace/bar"},
        workspace_id="ws", idx=0,
    )
    assert isinstance(repo, RepoMount)
    assert repo.base_source is None


def test_parse_repo_accepts_absolute_base_source():
    repo = _parse_repo(
        {
            "url": "git@github.com:foo/bar.git",
            "mount": "/workspace/bar",
            "base_source": "/home/user/bar",
        },
        workspace_id="ws", idx=0,
    )
    assert repo.base_source == "/home/user/bar"


def test_parse_repo_rejects_relative_base_source():
    with pytest.raises(WorkspaceSettingsError, match="base_source"):
        _parse_repo(
            {
                "url": "git@github.com:foo/bar.git",
                "mount": "/workspace/bar",
                "base_source": "relative/path",
            },
            workspace_id="ws", idx=0,
        )


def test_parse_repo_rejects_empty_base_source():
    with pytest.raises(WorkspaceSettingsError, match="base_source"):
        _parse_repo(
            {
                "url": "git@github.com:foo/bar.git",
                "mount": "/workspace/bar",
                "base_source": "",
            },
            workspace_id="ws", idx=0,
        )


def test_workspace_from_setting_propagates_repo_base_source():
    workspace = _workspace_from_setting(
        {
            "name": "Autonomy",
            "image": "autonomy-agent:latest",
            "repos": [
                {
                    "url": "git@github.com:foo/bar.git",
                    "mount": "/workspace/bar",
                    "base_source": "/home/user/bar",
                },
            ],
        },
        workspace_id="autonomy",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.repos[0].base_source == "/home/user/bar"


def test_workspace_from_setting_rejects_invalid_harness():
    with pytest.raises(WorkspaceSettingsError, match="invalid harness"):
        _workspace_from_setting(
            {
                "name": "Broken",
                "image": "autonomy-agent:dashboard",
                "harness": "bogus",
            },
            workspace_id="broken",
            graph_project="autonomy",
            artifacts=(),
            mounts={},
        )


# ── Capability resolution (auto-uqq0i) ──────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh empty scratch DB for capability resolution tests."""
    db = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("AUTONOMY_ORGS_DIR", raising=False)
    yield db


_GITHUB_IMPL = {
    "name": "autonomy/github",
    "version": 1,
    "implements": [{"contract": "source_control", "version": 1}],
    "delivery_mode": "image_baked",
    "package_root": "agents/capabilities/github",
    "probe": {"kind": "command", "entrypoint": "gh auth status"},
    "required_env": ["GH_TOKEN"],
    "skill_path": "agents/capabilities/github/SKILL.md",
    "primer_path": "agents/capabilities/github/primer.md",
}

_JIRA_IMPL = {
    "name": "autonomy/jira",
    "version": 1,
    "implements": [{"contract": "issue_tracker", "version": 1}],
    "delivery_mode": "mounted_tools",
    "package_root": "agents/capabilities/jira",
    "probe": {"kind": "command", "entrypoint": "jira-read --probe"},
    "required_env": ["JIRA_EMAIL", "JIRA_BASE_URL"],
    "required_secret_files": ["/run/secrets/jira_token"],
    "tool_paths": ["agents/capabilities/jira/tools"],
    "skill_path": "agents/capabilities/jira/SKILL.md",
    "primer_path": "agents/capabilities/jira/primer.md",
}


def _seed_github_install():
    ops.add_setting(
        CAPABILITY_IMPL_SET_ID, CAPABILITY_IMPL_REVISION,
        key="autonomy/github", payload=_GITHUB_IMPL, state="published",
     org=ops.CALLER_ORG)
    ops.add_setting(
        ORG_CAPABILITY_INSTALL_SET_ID, ORG_CAPABILITY_INSTALL_REVISION,
        key="source_control",
        payload={
            "contract": "source_control",
            "contract_version": 1,
            "implementation": "autonomy/github",
            "implementation_version": 1,
            "env_bindings": {"GH_TOKEN": "ghp_test_value"},
        },
     org=ops.CALLER_ORG)


def _seed_jira_install():
    ops.add_setting(
        CAPABILITY_IMPL_SET_ID, CAPABILITY_IMPL_REVISION,
        key="autonomy/jira", payload=_JIRA_IMPL, state="published",
     org=ops.CALLER_ORG)
    ops.add_setting(
        ORG_CAPABILITY_INSTALL_SET_ID, ORG_CAPABILITY_INSTALL_REVISION,
        key="issue_tracker",
        payload={
            "contract": "issue_tracker",
            "contract_version": 1,
            "implementation": "autonomy/jira",
            "implementation_version": 1,
            "env_bindings": {
                "JIRA_EMAIL": "ops@example.com",
                "JIRA_BASE_URL": "https://example.atlassian.net",
            },
            "secret_file_bindings": {
                "/run/secrets/jira_token": "/etc/autonomy/secrets/jira_token",
            },
        },
     org=ops.CALLER_ORG)


def _enable_capability(workspace_id: str, contract: str, *, enabled: bool = True):
    ops.add_setting(
        WORKSPACE_CAPABILITY_ENABLE_SET_ID,
        WORKSPACE_CAPABILITY_ENABLE_REVISION,
        key=f"{workspace_id}:{contract}",
        payload={"contract": contract, "enabled": enabled},
     org=ops.CALLER_ORG)


def test_resolve_capabilities_no_enables_returns_empty(graph_db_env):
    assert resolve_capabilities("ws-empty") == ()


def test_resolve_capabilities_walks_chain_to_implementation(graph_db_env):
    _seed_github_install()
    _enable_capability("dashboard", "source_control")

    caps = resolve_capabilities("dashboard")
    assert len(caps) == 1
    cap = caps[0]
    assert isinstance(cap, MaterializedCapability)
    assert cap.contract == "source_control"
    assert cap.contract_version == 1
    assert cap.implementation == "autonomy/github"
    assert cap.implementation_version == 1
    assert cap.delivery_mode == "image_baked"
    assert cap.package_root == "agents/capabilities/github"
    assert cap.mount_target == f"{CAPABILITIES_MOUNT_DIR}/autonomy-github"
    assert cap.required_env == ("GH_TOKEN",)
    assert cap.env_bindings == {"GH_TOKEN": "ghp_test_value"}
    assert cap.secret_file_bindings == {}


def test_resolve_capabilities_jira_carries_secret_file_binding(graph_db_env):
    _seed_jira_install()
    _enable_capability("ng", "issue_tracker")

    caps = resolve_capabilities("ng")
    assert len(caps) == 1
    cap = caps[0]
    assert cap.implementation == "autonomy/jira"
    assert cap.delivery_mode == "mounted_tools"
    assert cap.tool_paths == ("agents/capabilities/jira/tools",)
    assert cap.required_secret_files == ("/run/secrets/jira_token",)
    assert cap.secret_file_bindings == {
        "/run/secrets/jira_token": "/etc/autonomy/secrets/jira_token",
    }
    assert "JIRA_EMAIL" in cap.env_bindings
    assert "JIRA_BASE_URL" in cap.env_bindings


def test_resolve_capabilities_workspace_disable_overrides_org_install(graph_db_env):
    """Workspace can opt out of an org-installed capability — acceptance criterion."""
    _seed_jira_install()
    _enable_capability("ng", "issue_tracker", enabled=False)

    assert resolve_capabilities("ng") == ()


def test_resolve_capabilities_drops_when_no_workspace_enable(graph_db_env):
    """Org install alone is not enough — workspace must explicitly enable."""
    _seed_jira_install()
    # No workspace.capability.enable row written for any workspace.
    assert resolve_capabilities("ng") == ()


def test_resolve_capabilities_drops_when_install_missing(graph_db_env):
    """Enable without an org install resolves to nothing."""
    _enable_capability("dashboard", "source_control")
    assert resolve_capabilities("dashboard") == ()


def test_resolve_capabilities_filters_to_workspace_prefix(graph_db_env):
    """Enables for other workspaces must not bleed into this workspace."""
    _seed_github_install()
    _enable_capability("dashboard", "source_control")
    _enable_capability("other-ws", "source_control")

    dash = resolve_capabilities("dashboard")
    other = resolve_capabilities("other-ws")
    assert {c.contract for c in dash} == {"source_control"}
    assert {c.contract for c in other} == {"source_control"}
    # Each is only its own workspace's row.
    assert len(dash) == 1 and len(other) == 1


def test_resolve_capabilities_multiple_enabled_sorted_by_contract(graph_db_env):
    _seed_github_install()
    _seed_jira_install()
    _enable_capability("ng", "source_control")
    _enable_capability("ng", "issue_tracker")

    caps = resolve_capabilities("ng")
    assert [c.contract for c in caps] == ["issue_tracker", "source_control"]


def test_resolve_capabilities_drops_impl_not_implementing_contract_version(graph_db_env):
    """If the impl declares a different contract version, the chain breaks."""
    drift_impl = dict(_GITHUB_IMPL)
    drift_impl["implements"] = [{"contract": "source_control", "version": 2}]
    ops.add_setting(
        CAPABILITY_IMPL_SET_ID, CAPABILITY_IMPL_REVISION,
        key="autonomy/github", payload=drift_impl, state="published",
     org=ops.CALLER_ORG)
    ops.add_setting(
        ORG_CAPABILITY_INSTALL_SET_ID, ORG_CAPABILITY_INSTALL_REVISION,
        key="source_control",
        payload={
            "contract": "source_control",
            "contract_version": 1,
            "implementation": "autonomy/github",
            "implementation_version": 1,
        },
     org=ops.CALLER_ORG)
    _enable_capability("dashboard", "source_control")

    assert resolve_capabilities("dashboard") == ()


def test_impl_mount_target_replaces_slash_with_dash():
    """Impl names like ``autonomy/github`` must produce a path-safe slug."""
    assert _impl_mount_target("autonomy/github") == (
        f"{CAPABILITIES_MOUNT_DIR}/autonomy-github"
    )
