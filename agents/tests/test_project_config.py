"""Tests for agents.project_config — YAML project registry loader."""

import os
from pathlib import Path
from textwrap import dedent

import pytest

from agents import project_config as pc


@pytest.fixture(autouse=True)
def _reset_cache():
    pc.clear_cache()
    yield
    pc.clear_cache()


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "projects.yaml"
    p.write_text(dedent(body).lstrip())
    return p


# ── Parsing ──────────────────────────────────────────────────────

def test_loads_full_project(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          sample:
            name: "Sample Project"
            description: Sample project
            image: autonomy-agent:sample
            repos:
              - url: https://example.com/a.git
                mount: /workspace/a
                writable: true
              - url: https://example.com/b.git
                mount: /workspace/b
                writable: false
            working_dir: /workspace/a
            claude_md: CLAUDE.md
            startup: startup.sh
            dind: true
            graph_project: sample
            default_tags: [alpha, beta]
            dispatch_labels: [sample]
            env:
              FOO: bar
              BAZ: qux
    """)
    projects = pc.load_projects(path)

    assert set(projects) == {"sample"}
    p = projects["sample"]
    assert p.id == "sample"
    assert p.name == "Sample Project"
    assert p.description == "Sample project"
    assert p.image == "autonomy-agent:sample"
    assert p.working_dir == "/workspace/a"
    assert p.claude_md == "CLAUDE.md"
    assert p.startup == "startup.sh"
    assert p.dind is True
    assert p.graph_project == "sample"
    assert p.default_tags == ("alpha", "beta")
    assert p.dispatch_labels == ("sample",)
    assert p.env == {"FOO": "bar", "BAZ": "qux"}
    assert len(p.repos) == 2
    assert p.repos[0].url == "https://example.com/a.git"
    assert p.repos[0].mount == "/workspace/a"
    assert p.repos[0].writable is True
    assert p.repos[1].writable is False


def test_minimal_project_uses_defaults(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          minimal:
            image: img:tag
            graph_project: autonomy
    """)
    p = pc.load_projects(path)["minimal"]
    assert p.id == "minimal"
    assert p.name == "minimal"             # falls back to id when name absent
    assert p.description == ""
    assert p.repos == ()
    assert p.dind is False
    assert p.working_dir is None
    assert p.claude_md is None
    assert p.startup is None
    assert p.default_tags == ()
    assert p.dispatch_labels == ()
    assert p.env == {}


# ── Lookup ───────────────────────────────────────────────────────

def test_get_project_returns_single_entry(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          a:
            image: img:tag
            graph_project: autonomy
    """)
    assert pc.get_project("a", path=path).id == "a"


def test_get_project_unknown_raises(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          a:
            image: img:tag
            graph_project: autonomy
    """)
    with pytest.raises(KeyError, match="unknown project"):
        pc.get_project("missing", path=path)


# ── Validation ───────────────────────────────────────────────────

def test_missing_image_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          bad:
            graph_project: autonomy
    """)
    with pytest.raises(pc.ProjectConfigError, match="'image' is required"):
        pc.load_projects(path)


def test_missing_graph_project_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          bad:
            image: img:tag
    """)
    with pytest.raises(pc.ProjectConfigError, match="'graph_project' is required"):
        pc.load_projects(path)


def test_repo_missing_mount_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          bad:
            image: img:tag
            graph_project: autonomy
            repos:
              - url: https://example.com/a.git
    """)
    with pytest.raises(pc.ProjectConfigError, match="missing required field 'mount'"):
        pc.load_projects(path)


def test_top_level_must_have_projects_key(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text("other: stuff\n")
    with pytest.raises(pc.ProjectConfigError, match="'projects' mapping"):
        pc.load_projects(path)


def test_invalid_yaml_raises(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text("projects:\n  a: [unterminated\n")
    with pytest.raises(pc.ProjectConfigError, match="invalid YAML"):
        pc.load_projects(path)


def test_missing_file_errors(tmp_path):
    with pytest.raises(pc.ProjectConfigError, match="cannot stat"):
        pc.load_projects(tmp_path / "does-not-exist.yaml")


# ── Caching ──────────────────────────────────────────────────────

def test_cache_hit_when_mtime_unchanged(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          a:
            image: img:tag
            graph_project: autonomy
    """)
    first = pc.load_projects(path)
    second = pc.load_projects(path)
    assert first is second  # same dict object = cache hit


def test_cache_reloads_on_mtime_change(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          a:
            image: img:tag
            graph_project: autonomy
    """)
    first = pc.load_projects(path)
    assert set(first) == {"a"}

    path.write_text(dedent("""
        projects:
          b:
            image: img:tag
            graph_project: autonomy
    """).lstrip())
    newer = path.stat().st_mtime + 5
    os.utime(path, (newer, newer))

    second = pc.load_projects(path)
    assert set(second) == {"b"}
    assert first is not second


def test_force_bypasses_cache(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          a:
            image: img:tag
            graph_project: autonomy
    """)
    first = pc.load_projects(path)
    second = pc.load_projects(path, force=True)
    assert first is not second  # force=True always re-parses
    assert set(first) == set(second) == {"a"}


# ── Shipped config ───────────────────────────────────────────────

def test_shipped_config_has_expected_projects():
    projects = pc.load_projects(pc.DEFAULT_CONFIG_PATH)
    assert set(projects) == {"autonomy", "enterprise", "enterprise-ng"}


def test_shipped_config_autonomy():
    p = pc.get_project("autonomy", path=pc.DEFAULT_CONFIG_PATH)
    assert p.name == "Autonomy Network"
    assert p.image == "autonomy-agent:dashboard"
    assert p.working_dir == "/workspace/repo"
    assert p.claude_md == "agents/shared/terminal/CLAUDE.md"
    assert p.graph_project == "autonomy"
    assert p.default_tags == ()
    assert p.dispatch_labels == ()
    assert p.dind is False
    assert p.repos == ()
    assert p.startup is None


def test_shipped_config_enterprise():
    p = pc.get_project("enterprise", path=pc.DEFAULT_CONFIG_PATH)
    assert p.name == "Enterprise"
    assert p.image == "autonomy-agent:enterprise"
    assert p.working_dir == "/workspace/enterprise"
    assert p.claude_md == "agents/projects/enterprise/CLAUDE.md"
    assert p.startup == "agents/projects/enterprise/startup.sh"
    assert p.dind is True
    assert p.graph_project == "anchore"
    assert p.default_tags == ("enterprise",)
    assert p.dispatch_labels == ("enterprise",)
    mounts = {r.mount: r for r in p.repos}
    assert mounts["/workspace/enterprise"].url == "git@github.com:anchore/enterprise.git"
    assert mounts["/workspace/enterprise"].writable is True
    assert mounts["/workspace/enterprise_ng"].url == "git@github.com:anchore/enterprise_ng.git"
    assert mounts["/workspace/enterprise_ng"].writable is True


def test_shipped_config_enterprise_ng():
    p = pc.get_project("enterprise-ng", path=pc.DEFAULT_CONFIG_PATH)
    assert p.name == "Enterprise NG"
    assert p.image == "autonomy-agent:enterprise-ng"
    assert p.working_dir == "/workspace/enterprise_ng"
    assert p.claude_md == "agents/projects/enterprise-ng/CLAUDE.md"
    assert p.startup == "agents/projects/enterprise-ng/startup.sh"
    assert p.dind is True
    assert p.graph_project == "anchore"
    assert p.default_tags == ("enterprise", "enterprise-ng")
    assert p.dispatch_labels == ("enterprise-ng",)
    mounts = {r.mount: r for r in p.repos}
    assert mounts["/workspace/enterprise"].writable is False
    assert mounts["/workspace/enterprise_ng"].writable is True
