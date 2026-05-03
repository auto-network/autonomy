"""Tests for agents.primer_renderer — workspace runtime primer rendering.

Covers the conditional sections driven by WorkspaceV1 flags:
- writable / read-only repo listing
- background startup check
- docker-in-docker
- graph scoping (scope + tags)
- autonomy base runtime (graph, bd, agent-browser, CrossTalk)
"""

from __future__ import annotations

import pytest

from agents.primer_renderer import (
    PrimerOverlayDriftError,
    _find_overlay_writability_drift,
    render_workspace_primer,
)
from agents.workspace_settings import (
    CAPABILITIES_MOUNT_DIR,
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
        dind=False,
        default_tags=(),
        dispatch_labels=(),
        env={},
    )
    defaults.update(overrides)
    return WorkspaceV1(**defaults)


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
        repos=(RepoMount(url="u", mount="/workspace/ng", writable=True),),
    ))
    assert "`/workspace/ng` — **writable**" in out
    assert "## Editing and Committing" in out
    assert "## Limits" not in out


def test_readonly_repos_listed_as_readonly():
    out = render_workspace_primer(_cfg(
        repos=(RepoMount(url="u", mount="/workspace/a", writable=False),),
    ))
    assert "`/workspace/a` — read-only" in out
    assert "## Limits" in out
    assert "All mounted repos are **read-only**" in out
    assert "## Editing and Committing" not in out


def test_mixed_repos_both_sections():
    out = render_workspace_primer(_cfg(
        repos=(
            RepoMount(url="u1", mount="/workspace/enterprise", writable=False),
            RepoMount(url="u2", mount="/workspace/enterprise_ng", writable=True),
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
    out = render_workspace_primer(_cfg(dind=True))
    assert "## Docker-in-Docker" in out
    assert "docker compose" in out


def test_dind_section_omitted_when_disabled():
    out = render_workspace_primer(_cfg(dind=False))
    assert "## Docker-in-Docker" not in out


# ── Graph scoping ────────────────────────────────────────────────────

def test_graph_scope_env_var_rendered():
    out = render_workspace_primer(_cfg(graph_project="anchore"))
    assert "GRAPH_SCOPE=anchore" in out
    assert "**anchore** org" in out


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

    # 5. Correct GRAPH_SCOPE and GRAPH_TAGS
    assert "GRAPH_SCOPE=anchore" in out
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
    assert "GRAPH_SCOPE=autonomy" in out
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
    assert "GRAPH_SCOPE=anchore" in out
    assert "GRAPH_TAGS=enterprise,enterprise-v5" in out


# ── Output hygiene ───────────────────────────────────────────────────

def test_no_unrendered_template_syntax():
    """No `{{ }}`, `{%`, or other Jinja syntax should leak into the output."""
    out = render_workspace_primer(_cfg(
        startup="x", dind=True,
        default_tags=("a", "b"),
        repos=(
            RepoMount(url="u", mount="/workspace/a", writable=True),
            RepoMount(url="u", mount="/workspace/b", writable=False),
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


# ── Overlay / config drift detection (bead auto-yj7o) ───────────────

def test_drift_helper_flags_readonly_claim_on_writable_repo():
    cfg = _cfg(repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),))
    drift = _find_overlay_writability_drift(
        cfg, "Foo repo mounted read-only at `/workspace/foo`. Do not edit.\n"
    )
    assert drift
    assert "/workspace/foo" in drift[0]
    assert "writable=true" in drift[0]


def test_drift_helper_flags_writable_claim_on_readonly_repo():
    cfg = _cfg(repos=(RepoMount(url="u", mount="/workspace/foo", writable=False),))
    drift = _find_overlay_writability_drift(
        cfg, "Foo repo is writable at `/workspace/foo` — edit freely.\n"
    )
    assert drift
    assert "/workspace/foo" in drift[0]
    assert "writable=false" in drift[0]


def test_drift_helper_passes_when_overlay_agrees():
    cfg = _cfg(repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),))
    drift = _find_overlay_writability_drift(
        cfg, "Foo repo mounted writable at `/workspace/foo` — edit freely.\n"
    )
    assert drift == []


def test_drift_helper_skips_ambiguous_window():
    """If both words appear near the mount (e.g. contextual commentary),
    the overlay is not asserting a single claim — don't flag."""
    cfg = _cfg(repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),))
    drift = _find_overlay_writability_drift(
        cfg,
        "`/workspace/foo` is writable; dispatch a read-only bead for other repos.\n",
    )
    assert drift == []


def test_drift_helper_ignores_mount_prefix_collision():
    """`/workspace/enterprise` must not match a substring of
    `/workspace/enterprise_ng` when the latter is the only path mentioned."""
    cfg = _cfg(repos=(
        RepoMount(url="u", mount="/workspace/enterprise", writable=True),
    ))
    drift = _find_overlay_writability_drift(
        cfg, "NG repo mounted read-only at `/workspace/enterprise_ng`.\n"
    )
    assert drift == []


def test_drift_helper_ignores_overlay_with_no_mount_mentioned():
    cfg = _cfg(repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),))
    drift = _find_overlay_writability_drift(
        cfg, "General notes about the project with no mount paths.\n"
    )
    assert drift == []


def test_render_raises_when_overlay_contradicts_config(tmp_path, monkeypatch):
    """Intentionally-wrong overlay: config says writable=true, overlay says
    read-only. Render must fail rather than ship contradictory guidance."""
    monkeypatch.setattr("agents.primer_renderer.PROJECTS_DIR", tmp_path)
    (tmp_path / "sample").mkdir()
    (tmp_path / "sample" / "primer.md").write_text(
        "- **Foo repo**: mounted read-only at `/workspace/foo`. Do not edit.\n"
    )
    cfg = _cfg(
        id="sample",
        repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),),
    )
    with pytest.raises(PrimerOverlayDriftError) as exc:
        render_workspace_primer(cfg)
    assert "/workspace/foo" in str(exc.value)
    assert "sample" in str(exc.value)


def test_render_succeeds_when_overlay_agrees_with_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.primer_renderer.PROJECTS_DIR", tmp_path)
    (tmp_path / "sample").mkdir()
    (tmp_path / "sample" / "primer.md").write_text(
        "- **Foo repo**: mounted writable at `/workspace/foo`. Edit freely.\n"
    )
    cfg = _cfg(
        id="sample",
        repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),),
    )
    out = render_workspace_primer(cfg)
    assert "mounted writable at `/workspace/foo`" in out


def test_real_enterprise_ng_overlay_has_no_drift(shipped_workspaces):
    """Acceptance criterion for bead auto-yj7o: the real NG overlay must
    agree with projects.yaml after the fix."""
    # No exception = overlay no longer claims the enterprise repo is
    # read-only while projects.yaml sets writable=true.
    render_workspace_primer(get_workspace("enterprise-ng"))


# ── Org primer layer (bead auto-31i3) ───────────────────────────────

def test_org_primer_loaded_when_present(tmp_path, monkeypatch):
    """Workspaces in an org with an org primer file pick it up."""
    monkeypatch.setattr("agents.primer_renderer.ORGS_DIR", tmp_path)
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "primer.md").write_text(
        "### Acme conventions\n\n- Always use tabs\n"
    )
    out = render_workspace_primer(_cfg(graph_project="acme"))
    assert "## Org Conventions (acme)" in out
    assert "### Acme conventions" in out
    assert "Always use tabs" in out


def test_org_primer_missing_silently_skipped(tmp_path, monkeypatch):
    """Workspaces in an org without a primer file render without the section."""
    monkeypatch.setattr("agents.primer_renderer.ORGS_DIR", tmp_path)
    out = render_workspace_primer(_cfg(graph_project="no-such-org"))
    assert "## Org Conventions" not in out


def test_real_anchore_primer_appears_in_enterprise_workspaces(shipped_workspaces):
    """Acceptance criterion: both enterprise-ng and enterprise-v5 sessions
    see the Anchore org conventions without duplication."""
    ng = render_workspace_primer(get_workspace("enterprise-ng"))
    v5 = render_workspace_primer(get_workspace("enterprise-v5"))
    for out in (ng, v5):
        assert "## Org Conventions (anchore)" in out
        # Representative content from agents/orgs/anchore/primer.md
        assert "Pre-existing" in out
        assert "task lint" in out


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
    # The shipped placeholder primer content shows up.
    assert "GitHub capability — primer projection" in out


def test_disabled_capability_does_not_render():
    """Disabled (or absent) capabilities produce no primer section."""
    out = render_workspace_primer(_cfg(capabilities=()))
    assert "source_control@" not in out
    assert "issue_tracker@" not in out


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
        repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),),
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
        repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),),
        network_host=True,
    ))
    # Valid shell — DASHBOARD value followed by a real newline before curl.
    assert "DASHBOARD=https://localhost:8080\ncurl " in out
    # Sanity: the broken concatenation must not appear.
    assert "https://localhost:8080curl" not in out


def test_sync_snippet_renders_for_bridge_network():
    """Bridge-network workspaces resolve to host.docker.internal but the
    rendered shell must still place `curl` on its own line."""
    out = render_workspace_primer(_cfg(
        repos=(RepoMount(url="u", mount="/workspace/foo", writable=True),),
        network_host=False,
    ))
    assert "DASHBOARD=https://host.docker.internal:8080\ncurl " in out
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


# ── Turn-correction guidance (auto-edec1.5) ─────────────────


import json as _json  # noqa: E402

from tools.graph.schemas.turn_correction import (  # noqa: E402
    SCHEMA_REVISION as _TC_REV,
    SET_ID as _TC_SET_ID,
)


@pytest.fixture
def _graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file so primer Setting reads see it."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


def _write_tc_setting(workspace_id: str, payload: dict) -> None:
    """Helper: upsert an ``autonomy.workspace.turn_correction#1`` row."""
    from tools.graph import ops as _ops
    _ops.upsert_by_key(_TC_SET_ID, _TC_REV, workspace_id, payload)


def test_turn_correction_section_present_by_default(_graph_db_env):
    """No Setting → the renderer applies the safe defaults and still
    renders the section. The feature must be useful before any operator
    has authored a workspace-specific Setting.
    """
    out = render_workspace_primer(_cfg(id="sample"))
    assert "## Turn Corrections" in out
    # Defaults: enabled, balanced, do not persist accepts.
    assert "aggressiveness=balanced" in out
    assert "accepted corrections persist to the graph" not in out


def test_turn_correction_command_shape_is_canonical(_graph_db_env):
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
        "[--reason <text>] [--confidence <0..1>] --json"
    ) in out
    # The agent must NOT supply target_message_id or original_sha256.
    assert "target_message_id" in out and "original_sha256" in out
    assert "Do **not** supply" in out


def test_turn_correction_explains_full_replacement_semantics(_graph_db_env):
    """``corrected_text`` is the full corrected replacement message,
    not a span or a diff. Pin that wording so future renderer changes
    don't regress to "the changed span"."""
    out = render_workspace_primer(_cfg(id="sample"))
    assert "complete corrected replacement message" in out


def test_turn_correction_explains_session_side_resolution(_graph_db_env):
    """The primer must teach the workflow, not just the command name.

    Specifically: the session side (a) attaches the suggestion to the
    most likely nearby user turn, and (b) derives the guard hash. The
    agent doesn't supply either. The viewer renders the overlay; the
    operator may accept or dismiss it.
    """
    out = render_workspace_primer(_cfg(id="sample"))
    # Whitespace-normalized: markdown wrap may split phrases across lines.
    flat = " ".join(out.split())
    assert "session side" in flat
    assert "most likely nearby user turn" in flat
    assert "derives the guard hash" in flat
    assert "accept" in flat and "dismiss" in flat


def test_turn_correction_framed_around_perception_gaps(_graph_db_env):
    """Framing must be perception-gap reduction, NOT generic grammar cleanup."""
    out = render_workspace_primer(_cfg(id="sample"))
    assert "perception gap" in out
    # Explicit "not grammar pass" framing.
    assert "not** a grammar pass" in out


def test_turn_correction_includes_worked_example(_graph_db_env):
    """Acceptance criterion: one concrete worked example of immediate
    one-shot usage on a garbled user message must be present."""
    out = render_workspace_primer(_cfg(id="sample"))
    # The example shows the operator's garbled message...
    assert "wat are the implickatons of teh new auth fix" in out
    # ...and the agent's corrected, full-replacement response.
    assert (
        '"What are the implications of the new auth fix?"' in out
    ), "worked example must show full corrected replacement, not a span"


def test_turn_correction_off_mode_disables_volunteering(_graph_db_env):
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


def test_turn_correction_conservative_mode_distinct_wording(_graph_db_env):
    _write_tc_setting("sample", {"aggressiveness": "conservative"})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "aggressiveness=conservative" in out
    assert "clearly garbled" in out


def test_turn_correction_balanced_mode_distinct_wording(_graph_db_env):
    _write_tc_setting("sample", {"aggressiveness": "balanced"})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "aggressiveness=balanced" in out
    assert "suspect a perception gap" in out


def test_turn_correction_aggressive_mode_distinct_wording(_graph_db_env):
    _write_tc_setting("sample", {"aggressiveness": "aggressive"})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "aggressiveness=aggressive" in out
    assert "Err on the side" in out


def test_turn_correction_disabled_renders_explicit_note(_graph_db_env):
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


def test_turn_correction_persist_accepts_surfaces_in_status_line(_graph_db_env):
    """``persist_accepts_to_graph=true`` is surfaced in the primer's
    status line so the agent knows operator-accepted corrections will
    be persisted to the graph (in addition to the dashboard overlay).
    """
    _write_tc_setting("sample", {"persist_accepts_to_graph": True})
    out = render_workspace_primer(_cfg(id="sample"))
    assert "accepted corrections persist to the graph" in out


def test_turn_correction_per_workspace_isolation(_graph_db_env):
    """Setting keyed by workspace.id — each workspace reads its own row."""
    _write_tc_setting("sample-a", {"aggressiveness": "off"})
    _write_tc_setting("sample-b", {"aggressiveness": "aggressive"})
    a = render_workspace_primer(_cfg(id="sample-a"))
    b = render_workspace_primer(_cfg(id="sample-b"))
    assert "aggressiveness=off" in a
    assert "aggressiveness=aggressive" in b
    assert "aggressiveness=aggressive" not in a
    assert "aggressiveness=off" not in b


def test_turn_correction_instruction_template_override(_graph_db_env):
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


def test_turn_correction_command_hint_override(_graph_db_env):
    """``command_hint`` overrides the canonical command shape.

    Useful for workspaces that ship a wrapper around ``graph
    turn-correction suggest`` (e.g. via a capability tool path).
    """
    _write_tc_setting("sample", {
        "command_hint": "my-wrapper turn-correction --json",
    })
    out = render_workspace_primer(_cfg(id="sample"))
    assert "my-wrapper turn-correction --json" in out


def test_turn_correction_section_appears_after_bead_polishing(_graph_db_env):
    """Document order: bead polishing → turn corrections → working style.

    Pinned so future template changes don't accidentally split the
    block away from the rest of the agent-facing protocol guidance.
    """
    out = render_workspace_primer(_cfg(id="sample"))
    bp_idx = out.index("## Bead Polishing Protocol")
    tc_idx = out.index("## Turn Corrections")
    assert bp_idx < tc_idx

