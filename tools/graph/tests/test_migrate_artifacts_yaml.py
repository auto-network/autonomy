"""Tests for the yaml → ``autonomy.workspace.artifact#1`` migration.

Bead: auto-hhi23 (auto-txg5.S3). Spec: graph://0d3f750f-f9c.

Exercises ``build_plan`` and ``apply_migration`` against a synthetic
``projects.yaml`` fixture, plus the ``graph set members --caller-org``
CLI path that the acceptance criteria use to verify routing.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import textwrap
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from tools.graph import ops, cli
from tools.graph.db import GraphDB
from tools.graph.schemas import workspace_artifact  # noqa: F401
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS, SchemaValidationError
from tools.graph.migrations.migrate_artifacts_yaml import (
    ArtifactEntry,
    ArtifactMigrationError,
    MigrationPlan,
    SET_ID,
    SCHEMA_REVISION,
    apply_migration,
    build_plan,
    main as migrate_main,
)


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    """Snapshot + restore the global schema registry around each test.

    The workspace_artifact schema stays registered (it's re-added on import),
    but any test-defined schemas are evicted so they don't bleed between tests.
    """
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture
def synthetic_yaml(tmp_path: Path) -> Path:
    """A minimal projects.yaml with 3 workspaces across 2 orgs.

    Layout:
      - autonomy (graph_project=autonomy) — no artifacts
      - enterprise (graph_project=anchore) — 1 artifact
      - enterprise-ng (graph_project=anchore) — 3 artifacts
    """
    path = tmp_path / "projects.yaml"
    path.write_text(textwrap.dedent("""\
        orgs:
          autonomy: {name: "Autonomy"}
          anchore: {name: "Anchore"}
        projects:
          autonomy:
            name: "Autonomy Core"
            image: "auto:dev"
            graph_project: autonomy
            dispatch_labels: [dashboard]

          enterprise:
            name: "Enterprise"
            image: "ent:dev"
            graph_project: anchore
            artifacts:
              - name: license.yaml
                scope: personal-org
                required: true
                description: Anchore Enterprise license
                help: Generate at https://license.anchore.io

          enterprise-ng:
            name: "Enterprise NG"
            image: "entng:dev"
            graph_project: anchore
            artifacts:
              - name: license.yaml
                scope: personal-org
                required: true
                description: Anchore Enterprise license
                help: Generate at https://license.anchore.io
              - name: id_ed25519
                scope: personal-org
                required: true
                description: SSH key for Anchore private repos
              - name: workspace-config.json
                scope: personal-workspace
                required: false
        """))
    return path


@pytest.fixture
def orgs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "orgs"
    d.mkdir()
    return d


# ── build_plan ──────────────────────────────────────────────


def test_build_plan_enumerates_every_artifact(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    assert isinstance(plan, MigrationPlan)
    assert plan.yaml_path == synthetic_yaml

    keys = {(e.workspace, e.name, e.org) for e in plan.entries}
    assert keys == {
        ("enterprise", "license.yaml", "anchore"),
        ("enterprise-ng", "license.yaml", "anchore"),
        ("enterprise-ng", "id_ed25519", "anchore"),
        ("enterprise-ng", "workspace-config.json", "anchore"),
    }

    # autonomy had no artifacts, so it shouldn't appear in org_dbs.
    assert set(plan.org_dbs) == {"anchore"}
    assert plan.org_dbs["anchore"] == orgs_dir / "anchore.db"


def test_build_plan_composite_keys_drop_name_from_payload(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    licensed = next(
        e for e in plan.entries
        if e.workspace == "enterprise-ng" and e.name == "license.yaml"
    )
    assert licensed.key == "enterprise-ng:license.yaml"
    assert "name" not in licensed.payload
    assert "workspace" not in licensed.payload
    assert licensed.payload["scope"] == "personal-org"
    assert licensed.payload["required"] is True
    assert "description" in licensed.payload
    assert "help" in licensed.payload


def test_build_plan_optional_fields_absent_when_not_in_yaml(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    ng_cfg = next(
        e for e in plan.entries
        if e.name == "workspace-config.json"
    )
    # No description / help in yaml → absent from payload.
    assert "description" not in ng_cfg.payload
    assert "help" not in ng_cfg.payload
    assert ng_cfg.payload == {"scope": "personal-workspace", "required": False}


def test_build_plan_rejects_missing_graph_project(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("projects:\n  x:\n    image: 'x'\n    artifacts: []\n")
    with pytest.raises(ArtifactMigrationError, match="graph_project"):
        build_plan(bad)


def test_build_plan_rejects_artifact_without_scope(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        projects:
          x:
            image: 'x'
            graph_project: foo
            artifacts:
              - name: a.yaml
                required: true
        """))
    with pytest.raises(ArtifactMigrationError, match="scope"):
        build_plan(bad)


def test_build_plan_missing_yaml_is_error(tmp_path):
    with pytest.raises(ArtifactMigrationError, match="not found"):
        build_plan(tmp_path / "nope.yaml")


# ── apply_migration ────────────────────────────────────────


def test_apply_migration_inserts_canonical_settings(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    report = apply_migration(plan)
    assert report.dry_run is False
    assert report.inserted == 4
    assert report.already_present == 0

    # Verify by reading directly from the anchore org DB.
    db = GraphDB(orgs_dir / "anchore.db")
    try:
        rows = db.conn.execute(
            "SELECT set_id, schema_revision, key, payload, publication_state "
            "FROM settings WHERE set_id = ? ORDER BY key",
            (SET_ID,),
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 4
    for r in rows:
        assert r["set_id"] == SET_ID
        assert r["schema_revision"] == SCHEMA_REVISION
        assert r["publication_state"] == "canonical"
        payload = json.loads(r["payload"])
        assert "name" not in payload
        assert "workspace" not in payload

    ws_names = sorted(r["key"] for r in rows)
    assert ws_names == [
        "enterprise-ng:id_ed25519",
        "enterprise-ng:license.yaml",
        "enterprise-ng:workspace-config.json",
        "enterprise:license.yaml",
    ]


def test_apply_migration_is_idempotent(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    first = apply_migration(plan)
    assert first.inserted == 4

    # Second run: build a new plan; all keys already present → skipped.
    plan2 = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    second = apply_migration(plan2)
    assert second.inserted == 0
    assert second.already_present == 4
    assert {(ws, name, org) for ws, name, org in second.skipped} == {
        ("enterprise", "license.yaml", "anchore"),
        ("enterprise-ng", "license.yaml", "anchore"),
        ("enterprise-ng", "id_ed25519", "anchore"),
        ("enterprise-ng", "workspace-config.json", "anchore"),
    }


def test_apply_migration_dry_run_writes_nothing(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    report = apply_migration(plan, dry_run=True)
    assert report.dry_run is True
    assert report.inserted == 4

    # No DB should exist (and if it does, the settings table is empty).
    db_path = orgs_dir / "anchore.db"
    if db_path.exists():
        db = GraphDB(db_path)
        try:
            n = db.conn.execute(
                "SELECT COUNT(*) FROM settings WHERE set_id = ?", (SET_ID,),
            ).fetchone()[0]
            assert n == 0
        finally:
            db.close()


def test_apply_migration_rejects_unknown_scope(tmp_path, orgs_dir):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(textwrap.dedent("""\
        projects:
          x:
            image: 'x'
            graph_project: anchore
            artifacts:
              - name: a.yaml
                scope: not-a-real-scope
        """))
    plan = build_plan(bad_yaml, orgs_root=orgs_dir)
    with pytest.raises(SchemaValidationError, match="invalid scope"):
        apply_migration(plan)


# ── ops.read_set routed by caller_org ───────────────────────


def test_read_set_with_caller_org_returns_workspace_artifacts(
    synthetic_yaml, orgs_dir, monkeypatch,
):
    # Route resolve_caller_db_path() → orgs_dir via AUTONOMY_ORGS_DIR.
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)

    # Must exist for resolve_caller_db_path to return the per-org path.
    assert (orgs_dir / "anchore.db").exists()

    got = ops.read_set(SET_ID, caller_org="anchore")
    keys = sorted(m.key for m in got.members)
    assert keys == [
        "enterprise-ng:id_ed25519",
        "enterprise-ng:license.yaml",
        "enterprise-ng:workspace-config.json",
        "enterprise:license.yaml",
    ]

    # The autonomy org DB has no artifact Settings.
    autonomy_got = ops.read_set(SET_ID, caller_org="autonomy")
    assert autonomy_got.members == []


# ── graph set members --caller-org CLI (acceptance criterion) ──


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved_argv = sys.argv
    sys.argv = ["graph"] + argv
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved_argv
    return rc, out.getvalue(), err.getvalue()


def test_cli_set_members_caller_org_lists_artifacts(
    synthetic_yaml, orgs_dir, monkeypatch,
):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)

    rc, out, err = _run_cli([
        "set", "members", SET_ID, "--caller-org", "anchore",
    ])
    assert rc == 0, err
    # The key column in the CLI table truncates; check the unambiguous
    # prefix for each expected composite key.
    assert "enterprise:license.yaml" in out
    assert "enterprise-ng:license.yaml" in out
    assert "enterprise-ng:id_ed25519" in out
    assert "enterprise-ng:workspace-conf" in out  # truncated display

    # Same call with caller-org=autonomy routes to the empty autonomy DB.
    rc, out, err = _run_cli(["set", "members", SET_ID, "--caller-org", "autonomy"])
    assert rc == 0, err
    assert "no Settings in" in out


# ── CLI main() entry point ──────────────────────────────────


def test_migrate_main_dry_run(synthetic_yaml, orgs_dir, capsys):
    rc = migrate_main([
        "--yaml", str(synthetic_yaml),
        "--orgs-dir", str(orgs_dir),
        "--dry-run",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "entries planned:     4" in captured.out
    # Nothing should have landed in the anchore DB.
    db_path = orgs_dir / "anchore.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM settings WHERE set_id = ?", (SET_ID,),
            ).fetchone()[0]
            assert n == 0
        finally:
            conn.close()


def test_migrate_main_writes(synthetic_yaml, orgs_dir, capsys):
    rc = migrate_main([
        "--yaml", str(synthetic_yaml),
        "--orgs-dir", str(orgs_dir),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "WRITE" in captured.out
    assert "inserted:            4" in captured.out
    assert (orgs_dir / "anchore.db").exists()
