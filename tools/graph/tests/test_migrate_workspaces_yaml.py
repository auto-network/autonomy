"""Tests for the workspace yaml → autonomy.workspace#1 migration (auto-raycq).

Spec: graph://0d3f750f-f9c (Setting Primitive) + graph://eabec73c-baa
(Workspaces & Orgs). Covers:

* Synthetic yaml round-trips into the right org DB with the right payload.
* Idempotence — second run is a no-op.
* Target org DB missing → clear error.
* ``graph_project`` and ``artifacts`` are stripped from the payload.
* Payload validates against the registered schema.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tools.graph import schemas
from tools.graph.db import GraphDB
from tools.graph.migrations.migrate_workspaces_yaml import (
    MissingOrgDBError,
    WorkspaceMigrationError,
    apply_migration,
    build_plan,
    main as migrate_main,
)
from tools.graph.schemas.workspace import WORKSPACE_SET_ID


YAML_FIXTURE = textwrap.dedent(
    """
    projects:
      autonomy:
        name: "Autonomy Network"
        description: "Core platform"
        image: "autonomy-agent:dashboard"
        working_dir: "/workspace/repo"
        graph_project: autonomy
        default_tags: []
        dispatch_labels: [dashboard]
        dind: false

      enterprise:
        name: "Enterprise"
        description: "Anchore Enterprise"
        image: "autonomy-agent:enterprise"
        repos:
          - url: "git@github.com:anchore/enterprise.git"
            mount: "/workspace/enterprise"
            writable: true
        working_dir: "/workspace/enterprise"
        dind: true
        graph_project: anchore
        default_tags: [enterprise]
        dispatch_labels: [enterprise]

      enterprise-ng:
        name: "Enterprise NG"
        description: "Anchore Enterprise NG"
        image: "autonomy-agent:enterprise-ng"
        repos:
          - url: "git@github.com:anchore/enterprise.git"
            mount: "/workspace/enterprise"
            writable: true
          - url: "git@github.com:anchore/enterprise_ng.git"
            mount: "/workspace/enterprise_ng"
            writable: true
        working_dir: "/workspace/enterprise_ng"
        dind: true
        network_host: false
        graph_project: anchore
        default_tags: [enterprise, enterprise-ng]
        dispatch_labels: [enterprise-ng]
        env:
          ANCHORE_CONFIG_PATH: /workspace/enterprise_ng/config/default_config.yaml
        env_from_host:
          - GITHUB_RELEASE_PULL_TOKEN
        artifacts:
          - name: license.yaml
            scope: personal-org
            required: true
            description: Anchore Enterprise license
            help: "Get one from ..."
    """
).lstrip()


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def yaml_path(tmp_path) -> Path:
    p = tmp_path / "projects.yaml"
    p.write_text(YAML_FIXTURE)
    return p


@pytest.fixture
def orgs_dir(tmp_path) -> Path:
    """Per-org DB root with autonomy + anchore already bootstrapped."""
    d = tmp_path / "orgs"
    d.mkdir()
    for slug in ("autonomy", "anchore"):
        db = GraphDB.create_org_db(slug, type_="shared", path=d / f"{slug}.db")
        db.close()
    return d


def _settings_rows(org_db: Path) -> list[dict]:
    db = GraphDB(org_db)
    try:
        rows = db.conn.execute(
            "SELECT id, set_id, schema_revision, key, payload, "
            "publication_state FROM settings"
        ).fetchall()
    finally:
        db.close()
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "set_id": r["set_id"],
            "schema_revision": int(r["schema_revision"]),
            "key": r["key"],
            "payload": json.loads(r["payload"]),
            "publication_state": r["publication_state"],
        })
    return out


# ── Happy path ────────────────────────────────────────────


def test_migration_routes_to_correct_org_dbs(yaml_path, orgs_dir):
    report = build_plan(yaml_path, orgs_dir)
    apply_migration(report)

    autonomy_rows = _settings_rows(orgs_dir / "autonomy.db")
    anchore_rows = _settings_rows(orgs_dir / "anchore.db")

    autonomy_keys = {r["key"] for r in autonomy_rows}
    anchore_keys = {r["key"] for r in anchore_rows}

    assert autonomy_keys == {"autonomy"}
    assert anchore_keys == {"enterprise", "enterprise-ng"}


def test_migration_payload_shape(yaml_path, orgs_dir):
    apply_migration(build_plan(yaml_path, orgs_dir))
    rows = {r["key"]: r for r in _settings_rows(orgs_dir / "anchore.db")}

    eng = rows["enterprise-ng"]
    assert eng["set_id"] == WORKSPACE_SET_ID
    assert eng["schema_revision"] == 1
    assert eng["publication_state"] == "canonical"

    payload = eng["payload"]
    # Implicit fields NOT persisted.
    assert "graph_project" not in payload
    assert "org" not in payload
    # Artifacts are auto-S3's job.
    assert "artifacts" not in payload
    # Directly carried.
    assert payload["name"] == "Enterprise NG"
    assert payload["image"] == "autonomy-agent:enterprise-ng"
    assert payload["working_dir"] == "/workspace/enterprise_ng"
    assert payload["dind"] is True
    assert payload["network_host"] is False
    assert payload["dispatch_labels"] == ["enterprise-ng"]
    # default_tags → tags.
    assert payload["tags"] == ["enterprise", "enterprise-ng"]
    # Env preserved.
    assert payload["env"] == {
        "ANCHORE_CONFIG_PATH": (
            "/workspace/enterprise_ng/config/default_config.yaml"
        ),
    }
    assert payload["env_from_host"] == ["GITHUB_RELEASE_PULL_TOKEN"]
    # Repos normalized.
    assert payload["repos"] == [
        {
            "url": "git@github.com:anchore/enterprise.git",
            "mount": "/workspace/enterprise",
            "writable": True,
        },
        {
            "url": "git@github.com:anchore/enterprise_ng.git",
            "mount": "/workspace/enterprise_ng",
            "writable": True,
        },
    ]


def test_migrated_payload_validates_against_schema(yaml_path, orgs_dir):
    apply_migration(build_plan(yaml_path, orgs_dir))
    for slug in ("autonomy", "anchore"):
        for row in _settings_rows(orgs_dir / f"{slug}.db"):
            schemas.validate_payload(
                row["set_id"], row["schema_revision"], row["payload"],
            )  # no raise


def test_migration_is_idempotent(yaml_path, orgs_dir):
    apply_migration(build_plan(yaml_path, orgs_dir))
    before = {
        slug: [r["id"] for r in _settings_rows(orgs_dir / f"{slug}.db")]
        for slug in ("autonomy", "anchore")
    }
    # Second run → no-op, same ids.
    report2 = build_plan(yaml_path, orgs_dir)
    for entry in report2.entries:
        assert entry.action == "skip_exists"
    apply_migration(report2)
    after = {
        slug: [r["id"] for r in _settings_rows(orgs_dir / f"{slug}.db")]
        for slug in ("autonomy", "anchore")
    }
    assert before == after


# ── Error cases ───────────────────────────────────────────


def test_missing_org_db_surfaces_as_plan_error(tmp_path, yaml_path):
    # orgs_dir exists but has no autonomy.db / anchore.db.
    empty = tmp_path / "empty-orgs"
    empty.mkdir()
    report = build_plan(yaml_path, empty)
    errors = [e for e in report.entries if e.action == "error"]
    assert len(errors) == 3
    assert all("org DB not found" in e.reason for e in errors)

    with pytest.raises(MissingOrgDBError):
        apply_migration(report)


def test_missing_graph_project_raises(tmp_path, orgs_dir):
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent(
        """
        projects:
          bad:
            name: "Bad"
            image: "some/image"
        """
    ))
    with pytest.raises(WorkspaceMigrationError) as exc:
        build_plan(p, orgs_dir)
    assert "graph_project" in str(exc.value)


def test_missing_required_image_raises(tmp_path, orgs_dir):
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent(
        """
        projects:
          bad:
            name: "Bad"
            graph_project: autonomy
        """
    ))
    with pytest.raises(schemas.SchemaValidationError):
        build_plan(p, orgs_dir)


# ── Cross-org read (acceptance §3) ─────────────────────────


def test_read_set_returns_workspaces_per_org(yaml_path, orgs_dir,
                                                    monkeypatch):
    """Acceptance §3: ``read_set`` with the appropriate ``org``
    returns the workspaces living in that org's DB.

    Single-DB world (today): each call routes to exactly one DB — there
    is no cross-org union yet. Anchore's DB yields enterprise + enterprise-ng;
    autonomy's DB yields the autonomy workspace.
    """
    apply_migration(build_plan(yaml_path, orgs_dir))

    from tools.graph import settings_ops

    # Route settings_ops._open(org) through our per-org root by
    # pointing GRAPH_DB at the per-caller DB file.
    def _with_caller(slug: str):
        monkeypatch.setenv("GRAPH_DB", str(orgs_dir / f"{slug}.db"))
        return settings_ops.read_set("autonomy.workspace", org=slug)

    anchore = _with_caller("anchore")
    anchore_keys = sorted(m.key for m in anchore.members)
    assert anchore_keys == ["enterprise", "enterprise-ng"]

    autonomy = _with_caller("autonomy")
    autonomy_keys = sorted(m.key for m in autonomy.members)
    assert autonomy_keys == ["autonomy"]


# ── CLI entrypoint ────────────────────────────────────────


def test_cli_dry_run(yaml_path, orgs_dir, capsys):
    rc = migrate_main([
        "--projects-yaml", str(yaml_path),
        "--orgs-dir", str(orgs_dir),
        "--dry-run",
    ])
    assert rc == 0
    # No rows written.
    for slug in ("autonomy", "anchore"):
        assert _settings_rows(orgs_dir / f"{slug}.db") == []


def test_cli_apply(yaml_path, orgs_dir, capsys):
    rc = migrate_main([
        "--projects-yaml", str(yaml_path),
        "--orgs-dir", str(orgs_dir),
    ])
    assert rc == 0
    assert {r["key"] for r in _settings_rows(orgs_dir / "anchore.db")} == {
        "enterprise", "enterprise-ng",
    }
