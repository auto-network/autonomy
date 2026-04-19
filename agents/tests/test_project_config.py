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
    # claude_md is now rendered from the template; shipped configs leave
    # the optional override unset.
    assert p.claude_md is None
    assert p.graph_project == "autonomy"
    assert p.default_tags == ()
    assert p.dispatch_labels == ("dashboard",)
    assert p.dind is False
    assert p.repos == ()
    assert p.startup is None


def test_shipped_config_enterprise():
    p = pc.get_project("enterprise", path=pc.DEFAULT_CONFIG_PATH)
    assert p.name == "Enterprise"
    assert p.image == "autonomy-agent:enterprise"
    assert p.working_dir == "/workspace/enterprise"
    assert p.claude_md is None
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
    assert p.claude_md is None
    assert p.startup == "agents/projects/enterprise-ng/startup.sh"
    assert p.dind is True
    assert p.graph_project == "anchore"
    assert p.default_tags == ("enterprise", "enterprise-ng")
    assert p.dispatch_labels == ("enterprise-ng",)
    mounts = {r.mount: r for r in p.repos}
    assert mounts["/workspace/enterprise"].writable is False
    assert mounts["/workspace/enterprise_ng"].writable is True


def test_shipped_config_enterprise_ng_has_license_artifact():
    p = pc.get_project("enterprise-ng", path=pc.DEFAULT_CONFIG_PATH)
    assert len(p.artifacts) == 1
    art = p.artifacts[0]
    assert art.name == "license.yaml"
    assert art.scope == "personal-org"
    assert art.required is True
    assert "license" in art.description.lower()
    assert "license.anchore.io" in art.help


# ── Artifact parsing ──────────────────────────────────────────────

def test_artifacts_parse_all_fields(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          p:
            image: img:tag
            graph_project: org
            artifacts:
              - name: token
                scope: shared-workspace
                required: true
                description: API token
                help: Get from vault
    """)
    p = pc.load_projects(path)["p"]
    assert len(p.artifacts) == 1
    a = p.artifacts[0]
    assert a.name == "token"
    assert a.scope == "shared-workspace"
    assert a.required is True
    assert a.description == "API token"
    assert a.help == "Get from vault"


def test_artifacts_defaults(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          p:
            image: img:tag
            graph_project: org
            artifacts:
              - name: config.yaml
                scope: personal-org
    """)
    a = pc.load_projects(path)["p"].artifacts[0]
    assert a.required is False
    assert a.description == ""
    assert a.help == ""


def test_artifacts_absent_gives_empty_tuple(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          p:
            image: img:tag
            graph_project: org
    """)
    assert pc.load_projects(path)["p"].artifacts == ()


def test_artifact_invalid_scope_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          p:
            image: img:tag
            graph_project: org
            artifacts:
              - name: thing
                scope: bogus
    """)
    with pytest.raises(pc.ProjectConfigError, match="invalid scope"):
        pc.load_projects(path)


def test_artifact_missing_name_errors(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          p:
            image: img:tag
            graph_project: org
            artifacts:
              - scope: shared-org
    """)
    with pytest.raises(pc.ProjectConfigError, match="missing required field 'name'"):
        pc.load_projects(path)


def test_artifacts_must_be_list(tmp_path):
    path = _write_config(tmp_path, """
        projects:
          p:
            image: img:tag
            graph_project: org
            artifacts:
              name: token
              scope: shared-org
    """)
    with pytest.raises(pc.ProjectConfigError, match="'artifacts' must be a list"):
        pc.load_projects(path)


# ── Artifact path resolution ──────────────────────────────────────

def _proj(**overrides):
    defaults = dict(
        id="wks",
        name="wks",
        description="",
        image="img",
        graph_project="org",
    )
    defaults.update(overrides)
    return pc.ProjectConfig(**defaults)


def test_resolve_personal_org(tmp_path):
    p = _proj()
    art = pc.ArtifactSpec(name="license.yaml", scope="personal-org")
    assert pc.artifact_host_path(art, p, artifacts_root=tmp_path) == tmp_path / "personal" / "org" / "license.yaml"


def test_resolve_shared_org(tmp_path):
    p = _proj()
    art = pc.ArtifactSpec(name="ca.pem", scope="shared-org")
    assert pc.artifact_host_path(art, p, artifacts_root=tmp_path) == tmp_path / "shared" / "org" / "ca.pem"


def test_resolve_personal_workspace(tmp_path):
    p = _proj()
    art = pc.ArtifactSpec(name="token", scope="personal-workspace")
    assert pc.artifact_host_path(art, p, artifacts_root=tmp_path) == tmp_path / "personal" / "org" / "wks" / "token"


def test_resolve_shared_workspace(tmp_path):
    p = _proj()
    art = pc.ArtifactSpec(name="setup.yaml", scope="shared-workspace")
    assert pc.artifact_host_path(art, p, artifacts_root=tmp_path) == tmp_path / "shared" / "org" / "wks" / "setup.yaml"


# ── validate_artifacts ────────────────────────────────────────────

def test_validate_empty_when_no_artifacts(tmp_path):
    assert pc.validate_artifacts(_proj(), artifacts_root=tmp_path) == []


def test_validate_returns_missing_required(tmp_path):
    art = pc.ArtifactSpec(name="license.yaml", scope="personal-org", required=True, description="License", help="Get it")
    p = _proj(artifacts=(art,))
    missing = pc.validate_artifacts(p, artifacts_root=tmp_path)
    assert len(missing) == 1
    assert missing[0].artifact is art
    assert missing[0].path == tmp_path / "personal" / "org" / "license.yaml"
    assert missing[0].project_id == "wks"


def test_validate_skips_optional_missing(tmp_path):
    art = pc.ArtifactSpec(name="optional.yaml", scope="shared-org", required=False)
    p = _proj(artifacts=(art,))
    assert pc.validate_artifacts(p, artifacts_root=tmp_path) == []


def test_validate_passes_when_present(tmp_path):
    (tmp_path / "personal" / "org").mkdir(parents=True)
    (tmp_path / "personal" / "org" / "license.yaml").write_text("x")
    art = pc.ArtifactSpec(name="license.yaml", scope="personal-org", required=True)
    p = _proj(artifacts=(art,))
    assert pc.validate_artifacts(p, artifacts_root=tmp_path) == []


# ── artifact_mounts ───────────────────────────────────────────────

def test_mounts_skip_missing_optional(tmp_path):
    art = pc.ArtifactSpec(name="optional", scope="shared-org", required=False)
    p = _proj(artifacts=(art,))
    assert pc.artifact_mounts(p, artifacts_root=tmp_path) == {}


def test_mounts_include_present(tmp_path):
    (tmp_path / "personal" / "org").mkdir(parents=True)
    lic = tmp_path / "personal" / "org" / "license.yaml"
    lic.write_text("key: value")
    art = pc.ArtifactSpec(name="license.yaml", scope="personal-org", required=True)
    p = _proj(artifacts=(art,))
    mounts = pc.artifact_mounts(p, artifacts_root=tmp_path)
    assert mounts == {str(lic): "/etc/autonomy/artifacts/license.yaml:ro"}


def test_mounts_multiple_artifacts(tmp_path):
    (tmp_path / "personal" / "org").mkdir(parents=True)
    (tmp_path / "shared" / "org" / "wks").mkdir(parents=True)
    lic = tmp_path / "personal" / "org" / "license.yaml"
    lic.write_text("a")
    setup = tmp_path / "shared" / "org" / "wks" / "setup.json"
    setup.write_text("b")
    p = _proj(artifacts=(
        pc.ArtifactSpec(name="license.yaml", scope="personal-org", required=True),
        pc.ArtifactSpec(name="setup.json", scope="shared-workspace"),
    ))
    mounts = pc.artifact_mounts(p, artifacts_root=tmp_path)
    assert mounts == {
        str(lic): "/etc/autonomy/artifacts/license.yaml:ro",
        str(setup): "/etc/autonomy/artifacts/setup.json:ro",
    }


# ── error formatting ──────────────────────────────────────────────

def test_format_missing_artifact_error_uses_description_and_help(tmp_path):
    art = pc.ArtifactSpec(
        name="license.yaml",
        scope="personal-org",
        required=True,
        description="Anchore Enterprise license",
        help="Generate at https://license.anchore.io",
    )
    p = _proj(id="enterprise-ng", name="Enterprise NG", artifacts=(art,))
    missing = pc.MissingArtifact(
        artifact=art,
        path=tmp_path / "personal" / "org" / "license.yaml",
        project_id="enterprise-ng",
    )
    msg = pc.format_missing_artifact_error(missing, p)
    assert "Enterprise NG" in msg
    assert '"Anchore Enterprise license"' in msg
    assert "Expected at:" in msg
    assert "Help: Generate at https://license.anchore.io" in msg


def test_format_missing_artifact_error_falls_back_to_name(tmp_path):
    art = pc.ArtifactSpec(name="token", scope="shared-org", required=True)
    p = _proj(name="p", artifacts=(art,))
    m = pc.MissingArtifact(artifact=art, path=tmp_path / "x", project_id="p")
    msg = pc.format_missing_artifact_error(m, p)
    assert '"token"' in msg
    assert "Help:" not in msg
