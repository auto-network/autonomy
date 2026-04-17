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

def test_loads_container_project(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          sample:
            description: Sample project
            mode: container
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
    assert p.name == "sample"
    assert p.description == "Sample project"
    assert p.mode == "container"
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


def test_loads_host_project(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          host-dev:
            description: Host dev
            mode: host
            graph_project: autonomy
            default_tags: []
    """)
    projects = pc.load_projects(path)
    p = projects["host-dev"]
    assert p.mode == "host"
    assert p.image is None
    assert p.repos == ()
    assert p.working_dir is None
    assert p.startup is None
    assert p.dind is False
    assert p.graph_project == "autonomy"


def test_container_defaults(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          minimal:
            mode: container
            image: img:tag
            graph_project: autonomy
    """)
    p = pc.load_projects(path)["minimal"]
    assert p.repos == ()
    assert p.dind is False
    assert p.default_tags == ()
    assert p.dispatch_labels == ()
    assert p.env == {}


# ── Lookup ───────────────────────────────────────────────────────

def test_get_project_returns_single_entry(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          a:
            mode: host
            graph_project: autonomy
    """)
    assert pc.get_project("a", path=path).name == "a"


def test_get_project_unknown_raises(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          a:
            mode: host
            graph_project: autonomy
    """)
    with pytest.raises(KeyError, match="unknown project"):
        pc.get_project("missing", path=path)


# ── Validation ───────────────────────────────────────────────────

def test_container_without_image_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          bad:
            mode: container
            graph_project: autonomy
    """)
    with pytest.raises(pc.ProjectConfigError, match="requires a non-empty 'image'"):
        pc.load_projects(path)


def test_missing_graph_project_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          bad:
            mode: host
    """)
    with pytest.raises(pc.ProjectConfigError, match="graph_project is required"):
        pc.load_projects(path)


def test_invalid_mode_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          bad:
            mode: sidecar
            graph_project: autonomy
    """)
    with pytest.raises(pc.ProjectConfigError, match="mode must be"):
        pc.load_projects(path)


def test_repo_missing_mount_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          bad:
            mode: container
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
            mode: host
            graph_project: autonomy
    """)
    first = pc.load_projects(path)
    second = pc.load_projects(path)
    assert first is second  # same dict object = cache hit


def test_cache_reloads_on_mtime_change(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          a:
            mode: host
            graph_project: autonomy
    """)
    first = pc.load_projects(path)
    assert set(first) == {"a"}

    path.write_text(dedent("""
        projects:
          b:
            mode: host
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
            mode: host
            graph_project: autonomy
    """)
    first = pc.load_projects(path)
    second = pc.load_projects(path, force=True)
    assert first is not second  # force=True always re-parses
    assert set(first) == set(second) == {"a"}


# ── Shipped config ───────────────────────────────────────────────

def test_shipped_config_has_expected_projects():
    projects = pc.load_projects(pc.DEFAULT_CONFIG_PATH)
    assert {"autonomy", "autonomy-host", "enterprise", "enterprise-ng"}.issubset(projects)


def test_shipped_config_autonomy_is_read_only_container():
    p = pc.get_project("autonomy", path=pc.DEFAULT_CONFIG_PATH)
    assert p.mode == "container"
    assert p.graph_project == "autonomy"
    assert p.dind is False
    assert p.image == "autonomy-agent:dashboard"
    assert len(p.repos) == 1
    assert p.repos[0].writable is False


def test_shipped_config_autonomy_host_is_host_mode():
    p = pc.get_project("autonomy-host", path=pc.DEFAULT_CONFIG_PATH)
    assert p.mode == "host"
    assert p.graph_project == "autonomy"


def test_shipped_config_enterprise_is_dind_writable():
    p = pc.get_project("enterprise", path=pc.DEFAULT_CONFIG_PATH)
    assert p.mode == "container"
    assert p.dind is True
    assert p.graph_project == "anchore"
    assert p.default_tags == ("enterprise",)
    assert "enterprise" in p.dispatch_labels
    assert len(p.repos) == 1
    assert p.repos[0].writable is True


def test_shipped_config_enterprise_ng_has_both_repos():
    p = pc.get_project("enterprise-ng", path=pc.DEFAULT_CONFIG_PATH)
    assert p.mode == "container"
    assert p.dind is True
    assert p.graph_project == "anchore"
    assert p.default_tags == ("enterprise", "enterprise-ng")
    mounts = {r.mount: r for r in p.repos}
    assert "/workspace/enterprise-ng" in mounts
    assert mounts["/workspace/enterprise-ng"].writable is True
    assert "/workspace/enterprise" in mounts
    assert mounts["/workspace/enterprise"].writable is False
