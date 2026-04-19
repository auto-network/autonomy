"""Tests for agents.primer_renderer — workspace runtime primer rendering.

Covers the conditional sections driven by ProjectConfig flags:
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
from agents.project_config import ProjectConfig, RepoMount


def _cfg(**overrides) -> ProjectConfig:
    """Build a ProjectConfig with reasonable defaults for rendering tests."""
    defaults = dict(
        id="sample",
        name="Sample",
        description="Sample workspace",
        image="autonomy-agent:sample",
        graph_project="sample-org",
        repos=(),
        working_dir="/workspace/repo",
        claude_md=None,
        startup=None,
        dind=False,
        default_tags=(),
        dispatch_labels=(),
        env={},
    )
    defaults.update(overrides)
    return ProjectConfig(**defaults)


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

def test_enterprise_ng_shape():
    """Full integration: render for the real enterprise-ng workspace config
    and verify every acceptance-criterion-bearing section is present."""
    from agents.project_config import clear_cache, get_project
    clear_cache()
    out = render_workspace_primer(get_project("enterprise-ng"))

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


def test_autonomy_shape():
    """Default autonomy workspace: read-only, no DinD, no startup."""
    from agents.project_config import clear_cache, get_project
    clear_cache()
    out = render_workspace_primer(get_project("autonomy"))

    assert "## Docker-in-Docker" not in out
    assert "## Background Setup" not in out
    assert "## Editing and Committing" not in out
    assert "## Limits" in out
    assert "GRAPH_SCOPE=autonomy" in out
    # No default_tags for autonomy
    assert "GRAPH_TAGS=" not in out
    # Default host networking
    assert "`--network=host`" in out


def test_enterprise_shape():
    """Enterprise workspace: two writable repos, DinD, startup."""
    from agents.project_config import clear_cache, get_project
    clear_cache()
    out = render_workspace_primer(get_project("enterprise"))

    assert "## Docker-in-Docker" in out
    assert "## Background Setup" in out
    assert "## Editing and Committing" in out
    assert "`/workspace/enterprise` — **writable**" in out
    assert "`/workspace/enterprise_ng` — **writable**" in out
    assert "GRAPH_SCOPE=anchore" in out
    assert "GRAPH_TAGS=enterprise" in out


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


def test_real_enterprise_ng_overlay_has_no_drift():
    """Acceptance criterion for bead auto-yj7o: the real NG overlay must
    agree with projects.yaml after the fix."""
    from agents.project_config import clear_cache, get_project
    clear_cache()
    # No exception = overlay no longer claims the enterprise repo is
    # read-only while projects.yaml sets writable=true.
    render_workspace_primer(get_project("enterprise-ng"))
