"""Tests for agents.primer_renderer — workspace runtime primer rendering.

Covers the conditional sections driven by WorkspaceV1 flags:
- writable / read-only repo listing
- background startup check
- docker-in-docker
- graph scoping (scope + tags)
- autonomy base runtime (graph, bd, agent-browser, CrossTalk)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agents.primer_renderer import render_workspace_primer
from agents.workspace_settings import (
    CAPABILITIES_MOUNT_DIR,
    CapabilityToolTarget,
    MaterializedCapability,
    RepoMount,
    WorkspaceV1,
    get_workspace,
)


def _cfg(**overrides) -> WorkspaceV1:
    """Build a WorkspaceV1 with reasonable defaults for rendering tests."""
    defaults = dict(
        id="sample",
        name="Sample",
        description="Sample workspace",
        image="autonomy-agent:sample",
        graph_project="sample-org",
        repos=(),
        working_dir="/workspace/repo",
        startup=None,
        needs_nested_docker=False,
        default_tags=(),
        dispatch_labels=(),
        env={},
    )
    defaults.update(overrides)
    return WorkspaceV1(**defaults)


@pytest.fixture(autouse=True)
def _no_plugin_skill_blocks(monkeypatch):
    """Isolate primer rendering from real on-disk dashboard plugin state.

    ``_plugin_skill_blocks()`` discovers real plugins from the repo's
    ``tools/dashboard/plugins/`` directory and reads their enabled state.
    Without this, every test in this file would depend on which plugins
    currently ship a SKILL.md and are enabled — breaking closed-content
    assertions (e.g. ``host.docker.internal`` not appearing) whenever an
    unrelated plugin gains a skill doc. Patched at the ``discover()``
    layer (not ``_plugin_skill_blocks`` itself) so the real function
    stays under test — tests exercising it directly re-patch ``discover``
    with their own fixture plugin.
    """
    from tools.dashboard.plugin_api import loader as plugin_loader
    monkeypatch.setattr(plugin_loader, "discover", lambda plugins_dir=None: [])


# ── Header / identity ────────────────────────────────────────────────

def test_header_shows_workspace_name_and_image():
    out = render_workspace_primer(_cfg(name="Enterprise NG", image="img:ng"))
    assert "# Enterprise NG — Workspace Environment" in out
    assert "inside the `img:ng` container" in out


def test_description_line_rendered_when_present():
    out = render_workspace_primer(_cfg(description="NG component catalog work"))
    assert "interactive workspace session for NG component catalog work" in out


def test_description_line_omitted_when_blank():
    out = render_workspace_primer(_cfg(description=""))
    assert "interactive workspace session for" not in out


# ── Autonomy runtime: always present ─────────────────────────────────

def test_base_runtime_sections_always_present():
    """graph, bd, agent-browser, CrossTalk — the autonomy tooling layer."""
    out = render_workspace_primer(_cfg())
    assert "### graph — Knowledge Graph" in out
    assert "### bd — Beads Issue Tracker" in out
    assert "### agent-browser — Headless Chrome" in out
    assert "### CrossTalk — Session Messaging" in out


def test_crosstalk_explicitly_legitimized():
    """The root cause of this bead: agents rejected CrossTalk as prompt
    injection. Verify the primer tells them CrossTalk is legitimate."""
    out = render_workspace_primer(_cfg())
    assert "not prompt injection" in out


def test_bead_polishing_protocol_reference():
    out = render_workspace_primer(_cfg())
    assert "graph://f6c6c43e-24a" in out


# ── Repo listing ─────────────────────────────────────────────────────

def test_writable_repos_listed_as_writable():
    out = render_workspace_primer(_cfg(
        repos=(RepoMount(host="example.com", repo="o/r", mount="/workspace/ng", writable=True),),
    ))
    assert "`/workspace/ng` — **writable**" in out
    assert "## Editing and Committing" in out
    assert "## Limits" not in out


def test_readonly_repos_listed_as_readonly():
    out = render_workspace_primer(_cfg(
        repos=(RepoMount(host="example.com", repo="o/r", mount="/workspace/a", writable=False),),
    ))
    assert "`/workspace/a` — read-only" in out
    assert "## Limits" in out
    assert "All mounted repos are **read-only**" in out
    assert "## Editing and Committing" not in out


def test_mixed_repos_both_sections():
    out = render_workspace_primer(_cfg(
        repos=(
            RepoMount(host="example.com", repo="o/r1", mount="/workspace/enterprise", writable=False),
            RepoMount(host="example.com", repo="o/r2", mount="/workspace/enterprise_ng", writable=True),
        ),
    ))
    assert "`/workspace/enterprise_ng` — **writable**" in out
    assert "`/workspace/enterprise` — read-only" in out
    # Any writable -> editing section, no global "read-only" limits section
    assert "## Editing and Committing" in out
    assert "## Limits" not in out


def test_no_repos_defaults_to_readonly_autonomy():
    out = render_workspace_primer(_cfg(repos=()))
    # Fallback for the autonomy workspace (no explicit repos defined)
    assert "`/workspace/repo` — Autonomy Network source" in out
    assert "## Limits" in out


# ── Background setup ─────────────────────────────────────────────────

def test_background_setup_section_when_startup_defined():
    out = render_workspace_primer(_cfg(startup="agents/projects/ng/startup.sh"))
    assert "## Background Setup" in out
    assert ".setup-exit" in out
    assert ".setup.log" in out


def test_background_setup_section_omitted_when_no_startup():
    out = render_workspace_primer(_cfg(startup=None))
    assert "## Background Setup" not in out
    assert ".setup-exit" not in out


# ── DinD ─────────────────────────────────────────────────────────────

def test_dind_section_when_enabled():
    out = render_workspace_primer(_cfg(needs_nested_docker=True))
    assert "## Docker-in-Docker" in out
    assert "docker compose" in out


def test_dind_section_omitted_when_disabled():
    out = render_workspace_primer(_cfg(needs_nested_docker=False))
    assert "## Docker-in-Docker" not in out


# ── Graph scoping ────────────────────────────────────────────────────

def test_graph_scope_prose_names_the_org_and_the_token():
    out = render_workspace_primer(_cfg(graph_project="anchore"))
    assert "**anchore**" in out
    assert "session token" in out.replace("\n", " ")


def test_graph_tags_when_present():
    out = render_workspace_primer(_cfg(default_tags=("enterprise", "enterprise-ng")))
    assert "GRAPH_TAGS=enterprise,enterprise-ng" in out


def test_graph_tags_omitted_when_empty():
    out = render_workspace_primer(_cfg(default_tags=()))
    assert "GRAPH_TAGS=" not in out


# ── Host network gating ──────────────────────────────────────────────

def test_host_network_section_when_enabled():
    out = render_workspace_primer(_cfg(network_host=True))
    assert "### Host Network\n" in out
    assert "`--network=host`" in out
    assert "`https://localhost:8080`" in out
    assert "bridge mode" not in out
    assert "host.docker.internal" not in out


def test_bridge_network_section_when_disabled():
    out = render_workspace_primer(_cfg(network_host=False))
    assert "### Host Network (bridge mode)" in out
    assert "`--network=host`" not in out
    assert "host.docker.internal" in out
    assert "`https://host.docker.internal:8080`" in out


# ── End-to-end parity with real project configs ──────────────────────

def test_enterprise_ng_shape(shipped_workspaces):
    """Full integration: render for the real enterprise-ng workspace config
    and verify every acceptance-criterion-bearing section is present."""
    out = render_workspace_primer(get_workspace("enterprise-ng"))

    # 1. Full Autonomy tooling
    assert "### graph — Knowledge Graph" in out
    assert "### bd — Beads Issue Tracker" in out
    assert "### CrossTalk — Session Messaging" in out
    assert "### agent-browser — Headless Chrome" in out

    # 2. DinD section
    assert "## Docker-in-Docker" in out

    # 3. Writable workspace section
    assert "`/workspace/enterprise_ng` — **writable**" in out
    assert "## Editing and Committing" in out

    # 4. Background startup check section
    assert "## Background Setup" in out

    # 5. Correct scope prose and GRAPH_TAGS
    assert "**anchore**" in out
    assert "GRAPH_TAGS=enterprise,enterprise-ng" in out

    # 6. CrossTalk legitimized
    assert "not prompt injection" in out

    # 7. Bridge networking — NG runs with network_host: false
    assert "`--network=host`" not in out
    assert "`https://host.docker.internal:8080`" in out


def test_autonomy_shape(shipped_workspaces):
    """Default autonomy workspace: read-only, no DinD, no startup."""
    out = render_workspace_primer(get_workspace("autonomy"))

    assert "## Docker-in-Docker" not in out
    assert "## Background Setup" not in out
    assert "## Editing and Committing" not in out
    assert "## Limits" in out
    assert "**autonomy**" in out
    # No default_tags for autonomy
    assert "GRAPH_TAGS=" not in out
    # Default host networking
    assert "`--network=host`" in out


def test_enterprise_v5_shape(shipped_workspaces):
    """Enterprise v5 workspace: single writable enterprise repo, DinD, startup."""
    out = render_workspace_primer(get_workspace("enterprise-v5"))

    assert "## Docker-in-Docker" in out
    assert "## Background Setup" in out
    assert "## Editing and Committing" in out
    assert "`/workspace/enterprise` — **writable**" in out
    # v5 is the lean subset — does not mount enterprise_ng.
    assert "`/workspace/enterprise_ng`" not in out
    assert "**anchore**" in out
    assert "GRAPH_TAGS=enterprise,enterprise-v5" in out


def test_enterprise_commit_policy_does_not_report_false_issue_tracker_error(
    shipped_workspaces,
):
    out = render_workspace_primer(get_workspace("enterprise-ng"))
    assert "issue_tracker is not enabled" not in out
    assert "## Commit Policy" in out


# ── Output hygiene ───────────────────────────────────────────────────

def test_no_unrendered_template_syntax():
    """No `{{ }}`, `{%`, or other Jinja syntax should leak into the output."""
    out = render_workspace_primer(_cfg(
        startup="x", needs_nested_docker=True,
        default_tags=("a", "b"),
        repos=(
            RepoMount(host="example.com", repo="o/r", mount="/workspace/a", writable=True),
            RepoMount(host="example.com", repo="o/r", mount="/workspace/b", writable=False),
        ),
    ))
    assert "{{" not in out
    assert "{%" not in out
    assert "StrictUndefined" not in out


def test_output_is_non_trivial_markdown():
    """Sanity: the rendered primer is a substantial markdown document."""
    out = render_workspace_primer(_cfg())
    assert len(out) > 2000
    assert out.startswith("# ")


# ── Org primer layer (bead auto-31i3) ───────────────────────────────

def test_org_primer_missing_silently_skipped():
    """An org with no primer Setting renders without the org section."""
    out = render_workspace_primer(_cfg(graph_project="no-such-org"))
    assert "## Org Conventions" not in out


def test_real_anchore_primer_appears_in_enterprise_workspaces(shipped_workspaces):
    """Acceptance criterion: both enterprise-ng and enterprise-v5 sessions
    see the shared Anchore org primer — from its Setting, not a file — with
    no duplication."""
    _write_overlay(
        _ORG_PRIMER_SET_ID, _ORG_PRIMER_REV, "anchore",
        {"markdown": "### Anchore house style\n\n- `task lint` must pass.\n"},
        org="anchore",
    )
    ng = render_workspace_primer(get_workspace("enterprise-ng"))
    v5 = render_workspace_primer(get_workspace("enterprise-v5"))
    for out in (ng, v5):
        assert "## Org Conventions (anchore)" in out
        assert "task lint" in out
        assert out.count("## Org Conventions (anchore)") == 1


def test_autonomy_workspace_has_no_anchore_primer(shipped_workspaces):
    """Autonomy org has no primer file today — section must be absent."""
    out = render_workspace_primer(get_workspace("autonomy"))
    assert "## Org Conventions (anchore)" not in out


# ── Capability primer projection (auto-uqq0i) ───────────────────────


def _github_cap(*, primer_path="agents/capabilities/github/primer.md") -> MaterializedCapability:
    return MaterializedCapability(
        contract="source_control",
        contract_version=1,
        implementation="autonomy/github",
        implementation_version=1,
        delivery_mode="image_baked",
        package_root="agents/capabilities/github",
        mount_target=f"{CAPABILITIES_MOUNT_DIR}/autonomy-github",
        required_env=("GH_TOKEN",),
        primer_path=primer_path,
    )


def _jira_cap() -> MaterializedCapability:
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
    )


def test_no_capabilities_no_capability_section():
    out = render_workspace_primer(_cfg(capabilities=()))
    # No per-capability heading should appear when nothing is enabled.
    assert "autonomy/github" not in out
    assert "autonomy/jira" not in out


def test_enabled_capability_renders_primer_section():
    out = render_workspace_primer(_cfg(capabilities=(_github_cap(),)))
    # Per-capability heading carries the impl name and contract@version.
    assert "### autonomy/github — source_control@1" in out
    # Deterministic mount target announced to the agent.
    assert f"{CAPABILITIES_MOUNT_DIR}/autonomy-github" in out
    # The live capability primer content is embedded verbatim.
    assert "## GitHub capability" in out
    assert "`gh` is on PATH" in out


def test_capability_org_primer_appends_after_code_owned_base():
    cap = MaterializedCapability(
        **{
            **_jira_cap().__dict__,
            "org_primer": (
                "`Target Fix Versions` is `customfield_10172`; run "
                "`jira-fields KEY 'Target Fix'` for live values."
            ),
        }
    )
    out = render_workspace_primer(_cfg(
        graph_project="anchore", capabilities=(cap,),
    ))
    base_at = out.index("## Jira capability")
    org_at = out.index("#### Organization-specific guidance (anchore)")
    assert base_at < org_at
    assert "customfield_10172" in out


def test_disabled_capability_does_not_render():
    """Disabled (or absent) capabilities produce no primer section."""
    out = render_workspace_primer(_cfg(capabilities=()))
    assert "source_control@" not in out
    assert "issue_tracker@" not in out


def test_no_dashboard_apps_section_when_no_plugin_blocks():
    out = render_workspace_primer(_cfg())
    assert "Dashboard Apps" not in out


def test_dashboard_apps_section_renders_enabled_plugin_skill(monkeypatch):
    monkeypatch.setattr(
        "agents.primer_renderer._plugin_skill_blocks",
        lambda config: [
            {"id": "mission_control", "label": "Mission Control", "skill_text": "Push HTML here."},
        ],
    )
    out = render_workspace_primer(_cfg())
    assert "## Dashboard Apps" in out
    assert "### Mission Control (`mission_control`)" in out
    assert "Push HTML here." in out


def test_dashboard_apps_section_renders_multiple_plugins_in_order(monkeypatch):
    monkeypatch.setattr(
        "agents.primer_renderer._plugin_skill_blocks",
        lambda config: [
            {"id": "mission_control", "label": "Mission Control", "skill_text": "Mission doc."},
            {"id": "presentations", "label": "Present", "skill_text": "Present doc."},
        ],
    )
    out = render_workspace_primer(_cfg())
    mc_at = out.index("### Mission Control")
    present_at = out.index("### Present")
    assert mc_at < present_at


def test_plugin_skill_blocks_reads_manifest_and_skill_file(tmp_path, monkeypatch):
    """Unit test for _plugin_skill_blocks() itself against a fake plugin dir."""
    from agents import primer_renderer
    from tools.dashboard.plugin_api import loader as plugin_loader
    from tools.dashboard.plugin_api.manifest import PluginManifest

    plugin_dir = tmp_path / "sample_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "SKILL.md").write_text("How to use sample_plugin.\n")
    manifest = PluginManifest.model_validate({
        "id": "sample_plugin",
        "api_version": 1,
        "org": "autonomy",
        "paths": ["/sample-plugin"],
        "assets": {"template": "page.html", "script": "page.js"},
        "nav": {"label": "Sample Plugin"},
        "frontend": {"alpine_root": "samplePluginPage"},
        "skill": "SKILL.md",
    })
    discovered = plugin_loader.DiscoveredPlugin(manifest=manifest, plugin_dir=plugin_dir)

    monkeypatch.setattr(plugin_loader, "discover", lambda plugins_dir=None: [discovered])
    monkeypatch.setattr(plugin_loader, "_read_plugin_settings", lambda org=None: {})
    monkeypatch.setattr(plugin_loader, "is_enabled", lambda *a, **k: True)

    blocks = primer_renderer._plugin_skill_blocks(_cfg())
    assert blocks == [{
        "id": "sample_plugin", "label": "Sample Plugin",
        "skill_text": "How to use sample_plugin.",
    }]


def test_plugin_skill_blocks_skips_disabled_plugin(tmp_path, monkeypatch):
    from agents import primer_renderer
    from tools.dashboard.plugin_api import loader as plugin_loader
    from tools.dashboard.plugin_api.manifest import PluginManifest

    plugin_dir = tmp_path / "sample_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "SKILL.md").write_text("Should not appear.\n")
    manifest = PluginManifest.model_validate({
        "id": "sample_plugin",
        "api_version": 1,
        "org": "autonomy",
        "paths": ["/sample-plugin"],
        "assets": {"template": "page.html", "script": "page.js"},
        "nav": {"label": "Sample Plugin"},
        "frontend": {"alpine_root": "samplePluginPage"},
        "skill": "SKILL.md",
    })
    discovered = plugin_loader.DiscoveredPlugin(manifest=manifest, plugin_dir=plugin_dir)

    monkeypatch.setattr(plugin_loader, "discover", lambda plugins_dir=None: [discovered])
    monkeypatch.setattr(plugin_loader, "_read_plugin_settings", lambda org=None: {})
    monkeypatch.setattr(plugin_loader, "is_enabled", lambda *a, **k: False)

    assert primer_renderer._plugin_skill_blocks(_cfg()) == []


def test_plugin_skill_blocks_skips_plugin_without_skill_field(tmp_path, monkeypatch):
    from agents import primer_renderer
    from tools.dashboard.plugin_api import loader as plugin_loader
    from tools.dashboard.plugin_api.manifest import PluginManifest

    plugin_dir = tmp_path / "sample_plugin"
    plugin_dir.mkdir()
    manifest = PluginManifest.model_validate({
        "id": "sample_plugin",
        "api_version": 1,
        "org": "autonomy",
        "paths": ["/sample-plugin"],
        "assets": {"template": "page.html", "script": "page.js"},
        "nav": {"label": "Sample Plugin"},
        "frontend": {"alpine_root": "samplePluginPage"},
    })
    discovered = plugin_loader.DiscoveredPlugin(manifest=manifest, plugin_dir=plugin_dir)

    monkeypatch.setattr(plugin_loader, "discover", lambda plugins_dir=None: [discovered])
    monkeypatch.setattr(plugin_loader, "_read_plugin_settings", lambda org=None: {})
    monkeypatch.setattr(plugin_loader, "is_enabled", lambda *a, **k: True)

    assert primer_renderer._plugin_skill_blocks(_cfg()) == []


def test_multiple_capabilities_render_in_stable_order():
    """Capabilities render in the order they appear on the workspace.

    :class:`MaterializedCapability` rows are already sorted by contract
    name in the resolver; the renderer preserves that order so two
    successive launches produce identical primers.
    """
    out_a = render_workspace_primer(_cfg(
        capabilities=(_github_cap(), _jira_cap()),
    ))
    out_b = render_workspace_primer(_cfg(
        capabilities=(_github_cap(), _jira_cap()),
    ))
    assert out_a == out_b
    # Ordering as supplied: GitHub before Jira.
    gh_idx = out_a.index("### autonomy/github")
    ji_idx = out_a.index("### autonomy/jira")
    assert gh_idx < ji_idx


# ── Primer regressions found after auto-uqq0i (auto-1webn.2) ────────


def test_writable_session_branch_uses_session_prefix():
    """Worktrees create branches as `session/<name>` (per workspace_manager).

    The earlier `agent/<session>` wording is stale and confuses agents
    that look for the branch via tab-complete or `git branch --list`.
    """
    out = render_workspace_primer(_cfg(
        repos=(RepoMount(host="example.com", repo="o/r", mount="/workspace/foo", writable=True),),
    ))
    assert "session/<session>" in out, (
        "writable-branch guidance must reference the current "
        "`session/<session>` worktree branch model"
    )
    # The stale `agent/<session>` language must not survive in the
    # editing/committing block.
    assert "agent/<session>" not in out


def test_sync_snippet_assignment_and_curl_on_separate_lines():
    """The DASHBOARD assignment and the curl call must render on separate
    lines — the earlier template glued them together because Jinja's
    `trim_blocks=True` ate the trailing newline of the inline ``{% if %}``.
    """
    out = render_workspace_primer(_cfg(
        repos=(RepoMount(host="example.com", repo="o/r", mount="/workspace/foo", writable=True),),
        network_host=True,
    ))
    # Valid shell — DASHBOARD value followed by a real newline before the
    # next command (the template now interposes a REPO_NAME=$(curl ...)
    # lookup between the assignment and the sync-base curl).
    assert "DASHBOARD=https://localhost:8080\nREPO_NAME=$(curl " in out
    # Sanity: the broken concatenation must not appear.
    assert "https://localhost:8080REPO_NAME" not in out
    assert "https://localhost:8080curl" not in out


def test_sync_snippet_renders_for_bridge_network():
    """Bridge-network workspaces resolve to host.docker.internal but the
    rendered shell must still place `curl` on its own line."""
    out = render_workspace_primer(_cfg(
        repos=(RepoMount(host="example.com", repo="o/r", mount="/workspace/foo", writable=True),),
        network_host=False,
    ))
    assert "DASHBOARD=https://host.docker.internal:8080\nREPO_NAME=$(curl " in out
    assert "host.docker.internal:8080REPO_NAME" not in out
    assert "host.docker.internal:8080curl" not in out


def test_capability_with_missing_primer_file_still_renders_heading(tmp_path, monkeypatch):
    """A capability whose primer.md is missing on disk still gets a heading.

    The agent still needs to know the capability is enabled and where
    the package mount sits — we just skip the embedded primer body.
    """
    cap = _github_cap(primer_path="agents/capabilities/does-not-exist/primer.md")
    out = render_workspace_primer(_cfg(capabilities=(cap,)))
    assert "### autonomy/github — source_control@1" in out
    assert f"{CAPABILITIES_MOUNT_DIR}/autonomy-github" in out


# ── Workspace named queries in the capability block ─────────


_NAMED_QUERY_OVERRIDES = {
    "query_defaults": {"project": "ENTERPRISE"},
    "named_queries": [
        {"name": "mine", "summary": "Open tickets assigned to me",
         "query": "project = {project} AND assignee = currentUser()"},
        {"name": "release", "summary": "Tickets targeted at a release",
         "query": 'project = {project} AND fixVersion = "Enterprise {version}"'},
    ],
}


def _jira_cap_with_queries(*, expose=("jira-read", "jira-search", "jira-query")):
    cap = _jira_cap()
    return MaterializedCapability(
        **{**cap.__dict__,
           "tool_target": CapabilityToolTarget(
               source="agents/capabilities/jira/tools",
               target="/opt/jira-tools",
               expose_commands=expose),
           "workspace_overrides": _NAMED_QUERY_OVERRIDES})


def test_workspace_named_queries_render_in_capability_block():
    """The projection half of named queries: the workspace teaches the
    capability and the agent's context in one place — the enable Setting's
    workspace_overrides render as invocation lines in the primer."""
    out = render_workspace_primer(_cfg(
        id="enterprise-ng", capabilities=(_jira_cap_with_queries(),),
    ))
    assert "Workspace queries (enterprise-ng)" in out
    assert "jira-query mine" in out
    # Caller params derived from placeholders minus query_defaults.
    assert "jira-query release version=<value>" in out
    assert "# Open tickets assigned to me" in out
    assert "`jira-query --list`" in out


def test_named_queries_not_rendered_without_query_command():
    """An impl that predates named queries (no *-query command exposed)
    must not advertise an invocation that would fail."""
    out = render_workspace_primer(_cfg(
        id="enterprise-ng",
        capabilities=(_jira_cap_with_queries(expose=("jira-read",)),),
    ))
    assert "Workspace queries" not in out
    assert "jira-query mine" not in out


def test_no_named_queries_no_section():
    out = render_workspace_primer(_cfg(capabilities=(_jira_cap(),)))
    assert "Workspace queries" not in out


# ── Turn-correction guidance (auto-edec1.5) ─────────────────


from tools.graph.schemas.turn_correction import (  # noqa: E402
    SCHEMA_REVISION as _TC_REV,
    SET_ID as _TC_SET_ID,
)
from tools.graph.db import GraphDB  # noqa: E402
from tools.graph import db as _graph_db_mod  # noqa: E402


@pytest.fixture
def _turn_correction_org_env(tmp_path, monkeypatch):
    """Pin a fresh per-org Settings landscape for primer resolution tests."""
    orgs_root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_root))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("sample-org").close()
    GraphDB.create_org_db("other-org").close()
    try:
        yield orgs_root
    finally:
        GraphDB.close_all_pooled()


def _write_tc_setting(
    workspace_id: str,
    payload: dict,
    *,
    org: str = "sample-org",
) -> None:
    """Helper: upsert an ``autonomy.workspace.turn_correction#1`` row."""
    from tools.graph import ops as _ops
    _ops.upsert_by_key(_TC_SET_ID, _TC_REV, workspace_id, payload, org=org)


def test_turn_correction_section_present_by_default(_turn_correction_org_env):
    """No Setting → the renderer applies the safe defaults and still
    renders the section. The feature must be useful before any operator
    has authored a workspace-specific Setting.
    """
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Turn Corrections" in out
    # Defaults: enabled, aggressive, do not persist accepts.
    assert "aggressiveness=aggressive" in out
    assert "accepted corrections persist to the graph" not in out


def test_turn_correction_command_shape_is_canonical(_turn_correction_org_env):
    """Pin the exact v1 command shape an agent should emit.

    The session side derives ``target_message_id`` and the guard hash
    internally; the agent must NOT supply them. The primer guidance
    has to make the contract explicit so future agents stop inventing
    new flag shapes.
    """
    out = render_workspace_primer(_cfg(id="sample"))
    assert (
        "graph turn-correction suggest [corrected_text | --stdin] "
        "[--mode <off|conservative|balanced|aggressive>] "
        "[--reason <text>] [--confidence <0..1>]"
    ) in out
    # Delivery is the authenticated API, never a stdout-transport flag: the
    # turn-correction command line no longer carries --json.
    assert "[--confidence <0..1>] --json" not in out
    assert "--confidence 0.9 --json" not in out
    # The agent must NOT supply target_message_id or original_sha256.
    assert "target_message_id" in out and "original_sha256" in out
    assert "Do **not** supply" in out


def test_turn_correction_explains_full_replacement_semantics(_turn_correction_org_env):
    """``corrected_text`` is the full corrected replacement message,
    not a span or a diff. Pin that wording so future renderer changes
    don't regress to "the changed span"."""
    out = render_workspace_primer(_cfg(id="sample"))
    assert "full replacement message" in out


def test_turn_correction_output_is_a_redirect_safe_receipt(_turn_correction_org_env):
    """Delivery is the authenticated API call; the CLI's stdout is only a
    receipt and must be documented as safe to redirect or discard — the exact
    opposite of the old stdout-as-transport contract."""
    out = render_workspace_primer(_cfg(id="sample"))
    flat = " ".join(out.split())
    assert "receipt" in flat
    assert "authenticated dashboard API" in flat
    assert ">/dev/null" in out
    assert "safe to redirect" in flat
    # The stale stdout-transport language must be gone.
    assert "leave its output visible in the session log" not in flat
    assert "if that JSON is absent from the recorded tool result" not in flat


def test_turn_correction_explains_server_side_resolution(_turn_correction_org_env):
    """The primer must teach the workflow, not just the command name.

    Specifically: the dashboard (a) attaches the suggestion to the most likely
    nearby user turn, and (b) derives the guard hash server-side. The agent
    doesn't supply either. The viewer renders the overlay; the operator may
    accept or dismiss it.
    """
    out = render_workspace_primer(_cfg(id="sample"))
    # Whitespace-normalized: markdown wrap may split phrases across lines.
    flat = " ".join(out.split())
    assert "most likely nearby user turn" in flat
    assert "resolves them server-side" in flat
    assert "accept" in flat and "dismiss" in flat


def test_turn_correction_framed_around_perception_gaps(_turn_correction_org_env):
    """Framing must be perception-gap reduction, NOT generic grammar cleanup."""
    out = render_workspace_primer(_cfg(id="sample"))
    assert "shared-understanding hygiene" in out
    assert "push you into a guess" in out
    # Explicit "not copyediting" framing.
    assert "not** copyediting" in out


def test_turn_correction_includes_worked_example(_turn_correction_org_env):
    """Acceptance criterion: one concrete worked example of immediate
    one-shot usage on a garbled user message must be present."""
    out = render_workspace_primer(_cfg(id="sample"))
    # The example shows the operator's garbled message...
    assert "wat are the implickatons of teh new auth fix" in out
    # ...and the agent's corrected, full-replacement response.
    assert (
        '"What are the implications of the new auth fix?"' in out
    ), "worked example must show full corrected replacement, not a span"


def test_turn_correction_off_mode_disables_volunteering(_turn_correction_org_env):
    """``aggressiveness=off`` instructs the agent not to volunteer
    corrections. The command stays documented for reference so an
    explicit operator request still works.
    """
    _write_tc_setting("sample", {"aggressiveness": "off"})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "aggressiveness=off" in out
    assert "Do not volunteer" in out
    # Reference is preserved so the agent can still emit on request.
    assert "graph turn-correction suggest" in out


def test_turn_correction_conservative_mode_distinct_wording(_turn_correction_org_env):
    _write_tc_setting("sample", {"aggressiveness": "conservative"})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "aggressiveness=conservative" in out
    assert "clearly garbled or ambiguous enough" in out


def test_turn_correction_balanced_mode_distinct_wording(_turn_correction_org_env):
    _write_tc_setting("sample", {"aggressiveness": "balanced"})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "aggressiveness=balanced" in out
    assert "suspect a perception gap" in out
    assert "continue if the path forward is still clear" in out


def test_turn_correction_aggressive_mode_distinct_wording(_turn_correction_org_env):
    _write_tc_setting("sample", {"aggressiveness": "aggressive"})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "aggressiveness=aggressive" in out
    assert "Assume every user message is a candidate." in out
    assert "shared transcript or your understanding even slightly better" in out
    assert "Favor dictation, terminology, and ambiguity fixes" in out


def test_turn_correction_silent_and_action_biased_guidance_present(_turn_correction_org_env):
    out = render_workspace_primer(_cfg(id="sample"))
    flat = " ".join(out.split())
    assert "Do **not** talk about the correction." in out
    assert "keep working if the path forward is still clear" in flat
    assert "Only stop and acknowledge it when the meaning is too uncertain" in flat
    assert "Is this what you meant?" in out
    assert "Use this for communication, not just transcription" in flat
    assert "shared-understanding hygiene" in flat
    assert "make the user's meaning easier to act on" in flat
    assert "best reading visible before a misread turns into a bad reply or a bad log" in flat


def test_turn_correction_disabled_renders_explicit_note(_turn_correction_org_env):
    """``enabled=false`` swaps the body for a short "disabled" note.

    The block is still present so the agent knows the feature exists
    and is intentionally disabled rather than missing.
    """
    _write_tc_setting("sample", {"enabled": False})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Turn Corrections" in out
    assert "**disabled**" in out
    # The full workflow / command / example are gated on enabled.
    assert "wat are the implickatons" not in out


def test_turn_correction_persist_accepts_surfaces_in_status_line(_turn_correction_org_env):
    """``persist_accepts_to_graph=true`` is surfaced in the primer's
    status line so the agent knows operator-accepted corrections will
    be persisted to the graph (in addition to the dashboard overlay).
    """
    _write_tc_setting("sample", {"persist_accepts_to_graph": True})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "accepted corrections persist to the graph" in out


def test_turn_correction_per_workspace_isolation(_turn_correction_org_env):
    """Setting keyed by workspace.id — each workspace reads its own row."""
    _write_tc_setting("sample-a", {"aggressiveness": "off"})
    _write_tc_setting("sample-b", {"aggressiveness": "aggressive"})
    a = render_workspace_primer(_cfg(id="sample-a"))
    b = render_workspace_primer(_cfg(id="sample-b"))
    assert "aggressiveness=off" in a
    assert "aggressiveness=aggressive" in b
    assert "aggressiveness=aggressive" not in a
    assert "aggressiveness=off" not in b


def test_turn_correction_instruction_template_override(_turn_correction_org_env):
    """``instruction_template`` overrides the mode-specific lead-in.

    Operators can swap the wording without touching the renderer.
    """
    _write_tc_setting("sample", {
        "aggressiveness": "balanced",
        "instruction_template": "MARKER: emit corrections aggressively for this workspace.",
    })
    out = render_workspace_primer(_cfg(id="sample"))
    assert "MARKER: emit corrections aggressively for this workspace." in out
    # Default lead-in is suppressed when an override is supplied.
    assert "suspect a perception gap" not in out


def test_turn_correction_canonical_command_cannot_be_overridden(
    _turn_correction_org_env,
):
    """The primer must always include the exact v1 command shape.

    Settings can tune guidance wording, but they must not silently
    replace the canonical ``graph turn-correction suggest ... --json``
    contract.
    """
    _write_tc_setting("sample", {
        "instruction_template": "Custom wording is allowed.",
    })
    out = render_workspace_primer(_cfg(id="sample"))
    assert "Custom wording is allowed." in out
    assert "graph turn-correction suggest [corrected_text | --stdin]" in out
    assert "my-wrapper turn-correction --json" not in out


def test_turn_correction_reads_workspace_setting_from_owning_org(
    _turn_correction_org_env,
):
    """Workspace policy resolution is `(graph_project, workspace.id)`.

    A row written into a non-default org DB must still affect that
    workspace's primer, and rows from other orgs must not collide by
    shared key alone.
    """
    _write_tc_setting(
        "shared-workspace",
        {"aggressiveness": "aggressive"},
        org="other-org",
    )
    _write_tc_setting(
        "shared-workspace",
        {"aggressiveness": "off"},
        org="sample-org",
    )

    other = render_workspace_primer(
        _cfg(id="shared-workspace", graph_project="other-org")
    )
    sample = render_workspace_primer(
        _cfg(id="shared-workspace", graph_project="sample-org")
    )

    assert "aggressiveness=aggressive" in other
    assert "aggressiveness=off" not in other
    assert "aggressiveness=off" in sample
    assert "aggressiveness=aggressive" not in sample


def test_turn_correction_section_appears_after_bead_polishing(_turn_correction_org_env):
    """Document order: bead polishing → turn corrections → working style.

    Pinned so future template changes don't accidentally split the
    block away from the rest of the agent-facing protocol guidance.
    """
    out = render_workspace_primer(_cfg(id="sample"))
    bp_idx = out.index("## Bead Polishing Protocol")
    tc_idx = out.index("## Turn Corrections")
    assert bp_idx < tc_idx


# ── Cross-org resolvability of embedded pointers ─────────────────────

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "primers" / "workspace.md.j2"
)

# Both citation forms the template uses. Kept as two plain patterns
# rather than one alternation: a ``(?:`` group whose first branch starts
# with ``/`` silently fails to match here, which would make this guard
# quietly check nothing.
_ID = r"([0-9a-f]{6,}-[0-9a-f]{2,3})"
_POINTER_RES = (
    re.compile(r"graph://" + _ID),
    re.compile(r"graph\s+read\s+" + _ID),
)

# Architectural Signpost Index — published, long-lived, and not
# referenced by the template, so it works as a liveness probe for
# "is the real platform graph reachable from this process?"
_CONTROL_SOURCE_ID = "38c10838-094"


def _template_pointers() -> set[str]:
    """Every graph note id cited by the universal template."""
    text = TEMPLATE_PATH.read_text()
    found: set[str] = set()
    for rx in _POINTER_RES:
        found.update(rx.findall(text))
    return found


def test_template_pointers_are_peer_visible():
    """Template pointers must resolve from *any* org's seat.

    The template is the universal layer — it renders into the primer of
    every workspace in every org. Peer orgs see only the public surface
    of another org's DB (``publication_state`` in ``published`` /
    ``canonical``), so a pointer at ``raw`` or ``curated`` is a dead link
    for everyone outside the org that owns it.

    This enforces invariant 7 of the Primer Composition Contract
    (``graph://c07a3d75-bfb``): referenced IDs resolve.
    """
    from tools.graph import ops
    from tools.graph.cross_org import PEER_VISIBLE_STATES

    # Self-calibrate: the platform graph is only reachable where the real
    # org databases are mounted (the host). Containers and CI see either
    # no ``data/orgs`` at all or a stub holding just ``personal.db``, and
    # in-process reads there resolve nothing. Probe with a control note
    # that is independent of the template — the Architectural Signpost
    # Index — and skip rather than fail when it isn't there.
    if ops.get_source(_CONTROL_SOURCE_ID) is None:
        pytest.skip("platform graph not reachable in-process; host-only check")

    pointers = _template_pointers()
    assert pointers, "template should embed at least one graph:// pointer"

    unresolvable = {}
    for src_id in sorted(pointers):
        src = ops.get_source(src_id)
        if src is None:
            unresolvable[src_id] = "not found in any org"
            continue
        state = src.get("publication_state")
        if state not in PEER_VISIBLE_STATES:
            unresolvable[src_id] = f"publication_state={state!r}"

    assert not unresolvable, (
        "graph:// pointers in workspace.md.j2 are dead links for every "
        "non-owning org: "
        + "; ".join(f"{k} ({v})" for k, v in sorted(unresolvable.items()))
        + ". Fix with: graph promote <id> published"
    )


def test_platform_baseline_section_rendered():
    """Every org's primer points at the cross-org baseline index."""
    out = render_workspace_primer(_cfg())
    assert "## Platform Baseline" in out
    assert "graph://fedda572-5f4" in out


# ── Graph-native primer overlays (org + workspace) ───────────────────

from tools.graph.schemas.org_primer import (  # noqa: E402
    SCHEMA_REVISION as _ORG_PRIMER_REV,
    SET_ID as _ORG_PRIMER_SET_ID,
)
from tools.graph.schemas.workspace_primer import (  # noqa: E402
    SCHEMA_REVISION as _WS_PRIMER_REV,
    SET_ID as _WS_PRIMER_SET_ID,
)


def _write_overlay(set_id, rev, key, payload, org="sample-org"):
    from tools.graph import ops as _overlay_ops

    _overlay_ops.upsert_by_key(set_id, rev, key, payload, org=org)


def test_workspace_overlay_read_from_setting(_turn_correction_org_env):
    """A workspace overlay Setting renders without any file on disk."""
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample",
        {"markdown": "## Runbooks\n\nFleet status lives here."},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Runbooks" in out
    assert "Fleet status lives here." in out


def test_org_overlay_read_from_setting(_turn_correction_org_env):
    """An org overlay Setting renders under the generated heading."""
    _write_overlay(
        _ORG_PRIMER_SET_ID, _ORG_PRIMER_REV, "sample-org",
        {"markdown": "### House style\n\nAlways tee test output."},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Org Conventions (sample-org)" in out
    assert "Always tee test output." in out


def test_overlay_is_org_isolated(_turn_correction_org_env):
    """An overlay written in another org never renders here.

    This is the property the file layer could not provide: overlay
    content lives in the owning org's DB and is read with peers=[], so
    it cannot leak across an org boundary.
    """
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample",
        {"markdown": "## Secret Ops\n\nother org's runbook"},
        org="other-org",
    )
    out = render_workspace_primer(_cfg(id="sample", graph_project="sample-org"))
    assert "Secret Ops" not in out
    assert "other org's runbook" not in out


def test_disabled_overlay_is_skipped(_turn_correction_org_env):
    """enabled=false parks content without deleting the row."""
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample",
        {"markdown": "## Parked\n\nnot rendered", "enabled": False},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Parked" not in out


def test_customization_section_names_this_workspace(_turn_correction_org_env):
    """Every session is told how to change its own primer, concretely.

    The section is worthless if it shows placeholders — the agent must
    see its own workspace id and org in the commands it is meant to run.
    """
    out = render_workspace_primer(_cfg(id="sample", graph_project="sample-org"))
    assert "## Customizing This Primer" in out
    assert "autonomy.workspace.primer#1 --key sample:<block>" in out
    assert "X-Graph-Org: sample-org" in out
    assert "/api/primers/workspace/sample" in out


def test_customization_note_pointer_is_a_bare_id(_turn_correction_org_env):
    """The pointer must be pasteable into ``graph read`` as rendered.

    ``graph read`` rejects a ``graph://`` URI, so a scheme-prefixed
    constant would render a command that fails for every agent that
    follows it.
    """
    from agents.primer_renderer import PRIMER_CUSTOMIZATION_NOTE

    assert not PRIMER_CUSTOMIZATION_NOTE.startswith("graph://")
    out = render_workspace_primer(_cfg(id="sample"))
    assert f"graph read {PRIMER_CUSTOMIZATION_NOTE}" in out


def test_named_blocks_render_alongside_the_bare_row(_turn_correction_org_env):
    """A layer assembles from the bare key plus every ``key:block`` row."""
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample",
        {"markdown": "## Runbooks\n\nbase layer"},
    )
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample:escalation",
        {"markdown": "## Escalation\n\npage the operator"},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "base layer" in out
    assert "page the operator" in out


def test_one_block_parks_without_touching_the_others(_turn_correction_org_env):
    """The point of splitting a layer: park one section, keep the rest.

    This is what a single-row layer could not do — switching content off
    meant editing it out of the shared markdown blob and losing it.
    """
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample:keep",
        {"markdown": "## Keep\n\nstill rendering"},
    )
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample:park",
        {"markdown": "## Park\n\nparked text", "enabled": False},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "still rendering" in out
    assert "parked text" not in out
    assert "## Park" not in out


def test_blocks_sort_by_order_then_key(_turn_correction_org_env):
    """Explicit ``order`` wins; equal order falls back to key sequence."""
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample:zulu",
        {"markdown": "## Zulu\n\nfirst by order", "order": 10},
    )
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample:alpha",
        {"markdown": "## Alpha\n\ndefault order"},
    )
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample:bravo",
        {"markdown": "## Bravo\n\ndefault order too"},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert out.index("## Zulu") < out.index("## Alpha") < out.index("## Bravo")


def test_bare_row_leads_its_named_blocks(_turn_correction_org_env):
    """At equal order the unsuffixed row sorts ahead of ``key:block``."""
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample:aaa",
        {"markdown": "## Suffixed\n\nblock body"},
    )
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample",
        {"markdown": "## Bare\n\nbase body"},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert out.index("## Bare") < out.index("## Suffixed")


def test_blocks_do_not_leak_across_similarly_named_workspaces(
    _turn_correction_org_env,
):
    """``sample`` must not absorb blocks belonging to ``sample-two``.

    The prefix guard is ``key:``, not ``key``, so a workspace whose id
    is a string prefix of another's keeps its own blocks.
    """
    _write_overlay(
        _WS_PRIMER_SET_ID, _WS_PRIMER_REV, "sample-two:runbook",
        {"markdown": "## Neighbour\n\nnot mine"},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "not mine" not in out


def test_org_layer_also_supports_named_blocks(_turn_correction_org_env):
    """Block splitting is a property of both overlay layers, not just one."""
    _write_overlay(
        _ORG_PRIMER_SET_ID, _ORG_PRIMER_REV, "sample-org:style",
        {"markdown": "### House style\n\ntee your test output"},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Org Conventions (sample-org)" in out
    assert "tee your test output" in out


def test_all_blocks_disabled_emits_no_org_heading(_turn_correction_org_env):
    """Every block off must render like no rows at all — no bare heading."""
    _write_overlay(
        _ORG_PRIMER_SET_ID, _ORG_PRIMER_REV, "sample-org:style",
        {"markdown": "### Style\n\nparked", "enabled": False},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Org Conventions" not in out


def test_blank_org_overlay_emits_no_heading(_turn_correction_org_env):
    """An empty body must not render a bare 'Org Conventions' heading."""
    _write_overlay(
        _ORG_PRIMER_SET_ID, _ORG_PRIMER_REV, "sample-org", {"markdown": ""},
    )
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Org Conventions" not in out
