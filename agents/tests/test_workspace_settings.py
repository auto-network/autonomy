"""Focused tests for workspace Setting composition helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

import pytest

import agents.workspace_settings as workspace_settings
import agents.workspace_manager as workspace_manager

from agents.workspace_settings import (
    CAPABILITIES_MOUNT_DIR,
    MaterializedCapability,
    RepoMount,
    WorkspaceSettingsError,
    _impl_mount_target,
    _parse_repo,
    _compose_workspaces,
    _workspace_from_setting,
    invalidate_caches,
    invalidate_for_setting,
    load_workspaces,
    resolve_capabilities,
)
from tools.graph import ops
from tools.graph.schemas.capability_impl import (
    SET_ID as CAPABILITY_IMPL_SET_ID,
    SCHEMA_REVISION as CAPABILITY_IMPL_REVISION,
)
from tools.graph.schemas.capability_contract import (
    SET_ID as CAPABILITY_CONTRACT_SET_ID,
    SCHEMA_REVISION as CAPABILITY_CONTRACT_REVISION,
)
from tools.graph.schemas.org_capability_install import (
    SET_ID as ORG_CAPABILITY_INSTALL_SET_ID,
    SCHEMA_REVISION as ORG_CAPABILITY_INSTALL_REVISION,
)
from tools.graph.schemas.org_capability_primer import (
    SET_ID as ORG_CAPABILITY_PRIMER_SET_ID,
    SCHEMA_REVISION as ORG_CAPABILITY_PRIMER_REVISION,
)
from tools.graph.schemas.workspace_capability_enable import (
    SET_ID as WORKSPACE_CAPABILITY_ENABLE_SET_ID,
    SCHEMA_REVISION as WORKSPACE_CAPABILITY_ENABLE_REVISION,
)
from tools.graph.schemas.workspace import WORKSPACE_REVISION, WORKSPACE_SET_ID
from tools.graph.schemas.workspace_artifact import SET_ID as ARTIFACT_SET_ID
from tools.graph.schemas.mount import SET_ID as MOUNT_SET_ID


def test_workspace_cache_ignores_unrelated_setting_changes(monkeypatch):
    invalidate_caches()
    rebuilds = 0

    def rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return {"workspace": object()}

    monkeypatch.setattr(workspace_settings, "_load_workspaces_uncached", rebuild)

    load_workspaces()
    invalidate_for_setting("dashboard.harness.usage")
    load_workspaces()
    assert rebuilds == 1

    invalidate_for_setting(WORKSPACE_SET_ID)
    load_workspaces()
    assert rebuilds == 2
    invalidate_caches()


def test_workspace_cache_rebuild_is_singleflight(monkeypatch):
    invalidate_caches()
    rebuilds = 0

    def rebuild():
        nonlocal rebuilds
        rebuilds += 1
        time.sleep(0.03)
        return {"workspace": object()}

    monkeypatch.setattr(workspace_settings, "_load_workspaces_uncached", rebuild)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: load_workspaces(), range(8)))

    assert rebuilds == 1
    assert all(result is results[0] for result in results)
    invalidate_caches()


def test_workspace_composition_reads_dependent_sets_once_for_many_workspaces(
    monkeypatch,
):
    members = [
        SimpleNamespace(
            key="one", payload={"name": "One", "image": "image:one"}, org="autonomy",
        ),
        SimpleNamespace(
            key="two", payload={"name": "Two", "image": "image:two"}, org="autonomy",
        ),
    ]
    reads: list[str] = []

    def read_set(set_id, **_kwargs):
        reads.append(set_id)
        return SimpleNamespace(members=[])

    monkeypatch.setattr(workspace_settings.ops, "read_set", read_set)

    composed = _compose_workspaces(members, org="autonomy", graph_project="autonomy")

    assert set(composed) == {"one", "two"}
    assert reads.count(ARTIFACT_SET_ID) == 1
    assert reads.count(MOUNT_SET_ID) == 1
    assert reads.count(WORKSPACE_CAPABILITY_ENABLE_SET_ID) == 1


def test_workspace_from_setting_defaults_harness_to_claude():
    workspace = _workspace_from_setting(
        {"name": "Autonomy", "image": "autonomy-session-platform"},
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
            "image": "autonomy-session-platform",
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
            "image": "autonomy-session-platform",
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
            "image": "autonomy-session-dind",
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
            "image": "autonomy-session-dind",
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
        image="autonomy-session-dind",
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
            "image": "autonomy-session",
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
                "image": "autonomy-session-platform",
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
    """Hermetic per-org tree for capability resolution tests.

    Capability resolution reads enables/installs/impls at explicit org
    'autonomy', which a GRAPH_DB pin contradicts under the fail-loud
    resolver. Use the orgs tree instead: no pin, create the org DBs, and
    set GRAPH_ORG so the seed helpers' CALLER_ORG writes land in the same
    'autonomy' store the resolver reads.
    """
    from tools.graph.db import GraphDB
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    # The suite DECLARES its scope: bind the caller contextvar to the
    # personal store — the same mechanism the identity middleware uses for
    # a real caller — so seeds written at CALLER_ORG and reads at org=None
    # both resolve through the production sentinel path to one declared
    # place. Nothing here leans on an internal default; there isn't one
    # left for Settings writes.
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    from tools.graph import ops as _ops
    _tok = _ops.set_caller_org("personal")
    GraphDB.close_all_pooled()
    for slug, kind in (("autonomy", "shared"), ("personal", "personal")):
        GraphDB.create_org_db(slug, type_=kind, path=orgs / f"{slug}.db").close()
    GraphDB.close_all_pooled()
    yield orgs / "autonomy.db"
    _ops.reset_caller_org(_tok)
    GraphDB.close_all_pooled()


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


def _seed_contract(name: str, *, version: int = 1):
    ops.add_setting(
        CAPABILITY_CONTRACT_SET_ID, CAPABILITY_CONTRACT_REVISION,
        key=name,
        payload={
            "name": name,
            "version": version,
            "summary": f"{name} test contract",
            "ops": [{
                "name": "probe",
                "summary": "test operation",
                "input_schema": {},
                "output_schema": {},
            }],
        },
        state="published",
        org=ops.CALLER_ORG,
    )


def _seed_github_install():
    _seed_contract("source_control")
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
    _seed_contract("issue_tracker")
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


def test_resolve_capabilities_composes_org_capability_primer_blocks(graph_db_env):
    _seed_jira_install()
    _enable_capability("ng", "issue_tracker")
    ops.add_setting(
        ORG_CAPABILITY_PRIMER_SET_ID,
        ORG_CAPABILITY_PRIMER_REVISION,
        key="autonomy/jira:workflow",
        payload={"markdown": "Workflow guidance.", "order": 200},
        org=ops.CALLER_ORG,
    )
    ops.add_setting(
        ORG_CAPABILITY_PRIMER_SET_ID,
        ORG_CAPABILITY_PRIMER_REVISION,
        key="autonomy/jira:fields",
        payload={"markdown": "Field mappings.", "order": 100},
        org=ops.CALLER_ORG,
    )

    cap = resolve_capabilities("ng")[0]
    assert cap.org_primer == "Field mappings.\n\nWorkflow guidance."


def test_org_capability_primer_is_ignored_for_other_implementation(graph_db_env):
    _seed_jira_install()
    _enable_capability("ng", "issue_tracker")
    ops.add_setting(
        ORG_CAPABILITY_PRIMER_SET_ID,
        ORG_CAPABILITY_PRIMER_REVISION,
        key="autonomy/github:workflow",
        payload={"markdown": "GitHub only."},
        org=ops.CALLER_ORG,
    )

    assert resolve_capabilities("ng")[0].org_primer == ""


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


def test_resolve_capabilities_omits_when_install_missing(graph_db_env):
    """A missing install degrades the capability, never the base workspace."""
    _seed_contract("source_control")
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


def test_resolve_capabilities_omits_impl_not_implementing_contract_version(graph_db_env):
    """An incompatible implementation is unavailable without blocking launch."""
    _seed_contract("source_control")
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


def test_resolve_capabilities_omits_absent_pinned_implementation_version(graph_db_env):
    """The incident case: install pins v3 while the one implementation row is v5."""
    _seed_contract("source_control")
    current = dict(_GITHUB_IMPL, version=5)
    ops.add_setting(
        CAPABILITY_IMPL_SET_ID, CAPABILITY_IMPL_REVISION,
        key="autonomy/github", payload=current, state="published",
        org=ops.CALLER_ORG,
    )
    ops.add_setting(
        ORG_CAPABILITY_INSTALL_SET_ID, ORG_CAPABILITY_INSTALL_REVISION,
        key="source_control",
        payload={
            "contract": "source_control",
            "contract_version": 1,
            "implementation": "autonomy/github",
            "implementation_version": 3,
        },
        org=ops.CALLER_ORG,
    )
    _enable_capability("dashboard", "source_control")

    assert resolve_capabilities("dashboard") == ()


def test_broken_capability_stays_scoped_and_does_not_block_workspace_prepare(
    graph_db_env,
):
    """One bad capability remains diagnosable while its workspace can start."""
    _seed_contract("source_control")
    current = dict(_GITHUB_IMPL, version=5)
    ops.add_setting(
        CAPABILITY_IMPL_SET_ID, CAPABILITY_IMPL_REVISION,
        key="autonomy/github", payload=current, state="published",
        org=ops.CALLER_ORG,
    )
    ops.add_setting(
        ORG_CAPABILITY_INSTALL_SET_ID, ORG_CAPABILITY_INSTALL_REVISION,
        key="source_control",
        payload={
            "contract": "source_control",
            "contract_version": 1,
            "implementation": "autonomy/github",
            "implementation_version": 3,
        },
        org=ops.CALLER_ORG,
    )
    for workspace_id in ("broken", "healthy"):
        ops.add_setting(
            WORKSPACE_SET_ID, WORKSPACE_REVISION,
            key=workspace_id,
            payload={"name": workspace_id.title(), "image": "img"},
            org=ops.CALLER_ORG,
        )
    _enable_capability("broken", "source_control")
    invalidate_caches()

    workspaces = load_workspaces()

    assert set(workspaces) >= {"broken", "healthy"}
    assert workspaces["healthy"].capability_issues == ()
    assert [issue.subject for issue in workspaces["broken"].capability_issues] == [
        "autonomy/github@3",
    ]
    assert workspace_manager.prepare_session_mounts(
        workspaces["broken"], "test-session",
    ) == {}


def test_impl_mount_target_replaces_slash_with_dash():
    """Impl names like ``autonomy/github`` must produce a path-safe slug."""
    assert _impl_mount_target("autonomy/github") == (
        f"{CAPABILITIES_MOUNT_DIR}/autonomy-github"
    )


def test_workspace_from_setting_parses_host_root_mount_reason():
    workspace = _workspace_from_setting(
        {
            "name": "Ops",
            "image": "img",
            "host_root_mount": {"reason": "reads live dashboard state"},
        },
        workspace_id="ops",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.host_root_mount_reason == "reads live dashboard state"


def test_workspace_from_setting_defaults_to_no_host_root_mount():
    workspace = _workspace_from_setting(
        {"name": "Ops", "image": "img"},
        workspace_id="ops",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.host_root_mount_reason is None


@pytest.mark.parametrize("bad", [{}, {"reason": ""}, {"reason": "   "}, "yes"])
def test_workspace_from_setting_rejects_reasonless_host_root_mount(bad):
    with pytest.raises(WorkspaceSettingsError, match="reason"):
        _workspace_from_setting(
            {"name": "Ops", "image": "img", "host_root_mount": bad},
            workspace_id="ops",
            graph_project="autonomy",
            artifacts=(),
            mounts={},
        )
