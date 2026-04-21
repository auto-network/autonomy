"""Tests for the operator-local yaml → ``personal.db`` migration.

Bead: auto-gko4e (auto-txg5.S4). Spec: graph://0d3f750f-f9c.

Exercises ``build_plan`` and ``apply_migration`` against synthetic yaml
fixtures plus the ``graph set members --org personal`` CLI path
that the acceptance criteria use to verify routing.
"""

from __future__ import annotations

import io
import json
import sys
import textwrap
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from tools.graph import cli, ops
from tools.graph.db import GraphDB
from tools.graph.schemas import artifact_path, org_peer_subscription  # noqa: F401
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS, SchemaValidationError
from tools.graph.schemas.artifact_path import (
    SET_ID as ARTIFACT_PATH_SET_ID,
    SCHEMA_REVISION as ARTIFACT_PATH_REVISION,
)
from tools.graph.schemas.org_peer_subscription import (
    SET_ID as PEER_SUB_SET_ID,
    SCHEMA_REVISION as PEER_SUB_REVISION,
)
from tools.graph.migrations.migrate_operator_local import (
    OperatorLocalEntry,
    OperatorLocalMigrationError,
    MigrationPlan,
    PERSONAL_ORG_SLUG,
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
    """Snapshot + restore the global schema registry around each test."""
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
def orgs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "orgs"
    d.mkdir()
    return d


@pytest.fixture
def synthetic_yaml(tmp_path: Path) -> Path:
    """Operator yaml with both kinds of personal settings populated."""
    p = tmp_path / "operator.yaml"
    p.write_text(textwrap.dedent("""\
        artifact_paths:
          anchore:
            license.yaml: /home/op/licenses/anchore-license.yaml
            id_ed25519: /home/op/.ssh/id_ed25519
          autonomy:
            custom-artifact: /elsewhere/custom.json

        peer_subscriptions:
          autonomy:
            peers: [personal]
          personal:
            peers: []
        """))
    return p


@pytest.fixture
def empty_yaml(tmp_path: Path) -> Path:
    """Yaml that has no operator-local config sections (current shipped shape)."""
    p = tmp_path / "empty.yaml"
    p.write_text(textwrap.dedent("""\
        orgs:
          autonomy:
            name: "Autonomy Network"
        projects:
          autonomy:
            name: "Autonomy"
            image: auto:dev
            graph_project: autonomy
        """))
    return p


def _settings_rows(db_path: Path) -> list[dict]:
    db = GraphDB(db_path)
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


# ── build_plan ──────────────────────────────────────────────


def test_build_plan_enumerates_all_operator_sections(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    assert isinstance(plan, MigrationPlan)
    assert plan.personal_db == orgs_dir / "personal.db"

    by_key = {(e.set_id, e.key): e for e in plan.entries}
    assert (ARTIFACT_PATH_SET_ID, "anchore:license.yaml") in by_key
    assert (ARTIFACT_PATH_SET_ID, "anchore:id_ed25519") in by_key
    assert (ARTIFACT_PATH_SET_ID, "autonomy:custom-artifact") in by_key
    assert (PEER_SUB_SET_ID, "autonomy") in by_key
    assert (PEER_SUB_SET_ID, "personal") in by_key
    assert len(plan.entries) == 5


def test_build_plan_artifact_path_payload_shape(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    entry = next(
        e for e in plan.entries
        if e.set_id == ARTIFACT_PATH_SET_ID
        and e.key == "anchore:license.yaml"
    )
    assert entry.schema_revision == ARTIFACT_PATH_REVISION
    assert entry.payload == {"path": "/home/op/licenses/anchore-license.yaml"}


def test_build_plan_peer_subscription_payload_shape(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    autonomy_sub = next(
        e for e in plan.entries
        if e.set_id == PEER_SUB_SET_ID and e.key == "autonomy"
    )
    assert autonomy_sub.payload == {"peers": ["personal"]}
    isolated = next(
        e for e in plan.entries
        if e.set_id == PEER_SUB_SET_ID and e.key == "personal"
    )
    assert isolated.payload == {"peers": []}


def test_build_plan_empty_yaml_yields_nothing(empty_yaml, orgs_dir):
    plan = build_plan(empty_yaml, orgs_root=orgs_dir)
    assert plan.entries == []
    # Still records the target DB for display / apply.
    assert plan.personal_db == orgs_dir / "personal.db"


def test_build_plan_missing_yaml_yields_empty_plan(tmp_path, orgs_dir):
    missing = tmp_path / "does-not-exist.yaml"
    plan = build_plan(missing, orgs_root=orgs_dir)
    assert plan.entries == []
    assert plan.yaml_path == missing


def test_build_plan_no_yaml_yields_empty_plan(orgs_dir):
    plan = build_plan(None, orgs_root=orgs_dir)
    assert plan.entries == []
    assert plan.yaml_path is None


def test_build_plan_rejects_non_mapping_artifact_paths(tmp_path, orgs_dir):
    bad = tmp_path / "bad.yaml"
    bad.write_text("artifact_paths: [not-a-mapping]\n")
    with pytest.raises(OperatorLocalMigrationError, match="artifact_paths"):
        build_plan(bad, orgs_root=orgs_dir)


def test_build_plan_rejects_non_string_path(tmp_path, orgs_dir):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        artifact_paths:
          anchore:
            license.yaml: 12345
        """))
    with pytest.raises(OperatorLocalMigrationError, match="path must be"):
        build_plan(bad, orgs_root=orgs_dir)


def test_build_plan_rejects_non_list_peers(tmp_path, orgs_dir):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        peer_subscriptions:
          autonomy:
            peers: "not-a-list"
        """))
    with pytest.raises(OperatorLocalMigrationError, match="must be a list"):
        build_plan(bad, orgs_root=orgs_dir)


def test_build_plan_treats_bare_caller_as_isolated(tmp_path, orgs_dir):
    """`caller: ~` (yaml null) is treated as peers=[]."""
    p = tmp_path / "bare.yaml"
    p.write_text(textwrap.dedent("""\
        peer_subscriptions:
          personal: ~
        """))
    plan = build_plan(p, orgs_root=orgs_dir)
    assert len(plan.entries) == 1
    assert plan.entries[0].set_id == PEER_SUB_SET_ID
    assert plan.entries[0].key == "personal"
    assert plan.entries[0].payload == {"peers": []}


def test_build_plan_rejects_unknown_artifact_shape(tmp_path, orgs_dir):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        artifact_paths:
          "":
            license.yaml: /p
        """))
    with pytest.raises(OperatorLocalMigrationError, match="non-empty"):
        build_plan(bad, orgs_root=orgs_dir)


# ── apply_migration ────────────────────────────────────────


def test_apply_migration_inserts_canonical_settings(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)

    assert (orgs_dir / "personal.db").exists()
    rows = _settings_rows(orgs_dir / "personal.db")
    # Rows from ensure_bootstrap_orgs (identity Setting) plus our entries.
    artifact_rows = [r for r in rows if r["set_id"] == ARTIFACT_PATH_SET_ID]
    peer_rows = [r for r in rows if r["set_id"] == PEER_SUB_SET_ID]
    assert len(artifact_rows) == 3
    assert len(peer_rows) == 2

    for r in artifact_rows + peer_rows:
        assert r["publication_state"] == "canonical"

    by_key = {r["key"]: r for r in artifact_rows}
    assert by_key["anchore:license.yaml"]["payload"] == {
        "path": "/home/op/licenses/anchore-license.yaml",
    }
    assert by_key["anchore:id_ed25519"]["payload"] == {
        "path": "/home/op/.ssh/id_ed25519",
    }
    assert by_key["autonomy:custom-artifact"]["payload"] == {
        "path": "/elsewhere/custom.json",
    }

    peer_by_key = {r["key"]: r for r in peer_rows}
    assert peer_by_key["autonomy"]["payload"] == {"peers": ["personal"]}
    assert peer_by_key["personal"]["payload"] == {"peers": []}


def test_apply_migration_is_idempotent(synthetic_yaml, orgs_dir):
    # First run: five inserts.
    plan1 = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan1)
    assert len(plan1.inserted) == 5
    assert len(plan1.skipped) == 0

    # Second run: all skipped.
    plan2 = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan2)
    assert len(plan2.inserted) == 0
    assert len(plan2.skipped) == 5


def test_apply_migration_dry_run_writes_nothing(synthetic_yaml, orgs_dir):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan, dry_run=True)

    if (orgs_dir / "personal.db").exists():
        rows = _settings_rows(orgs_dir / "personal.db")
        assert not any(
            r["set_id"] in (ARTIFACT_PATH_SET_ID, PEER_SUB_SET_ID)
            for r in rows
        )


def test_apply_migration_empty_plan_is_noop(empty_yaml, orgs_dir):
    plan = build_plan(empty_yaml, orgs_root=orgs_dir)
    apply_migration(plan)
    # personal.db should not be materialised on an empty plan — the
    # migration has nothing to commit.
    assert not (orgs_dir / "personal.db").exists()


def test_apply_migration_bootstraps_personal_db_when_absent(
    synthetic_yaml, orgs_dir,
):
    assert not (orgs_dir / "personal.db").exists()
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)
    assert (orgs_dir / "personal.db").exists()


def test_apply_migration_payload_validates_against_schema(
    synthetic_yaml, orgs_dir,
):
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)
    from tools.graph import schemas
    for row in _settings_rows(orgs_dir / "personal.db"):
        if row["set_id"] not in (ARTIFACT_PATH_SET_ID, PEER_SUB_SET_ID):
            continue
        schemas.validate_payload(
            row["set_id"], row["schema_revision"], row["payload"],
        )  # no raise


# ── Schema-level direct validation ─────────────────────────


def test_artifact_path_schema_rejects_empty_path():
    from tools.graph import schemas
    with pytest.raises(SchemaValidationError, match="'path' must"):
        schemas.validate_payload(
            ARTIFACT_PATH_SET_ID, ARTIFACT_PATH_REVISION, {"path": ""},
        )


def test_artifact_path_schema_rejects_missing_path():
    from tools.graph import schemas
    with pytest.raises(SchemaValidationError, match="'path' is required"):
        schemas.validate_payload(
            ARTIFACT_PATH_SET_ID, ARTIFACT_PATH_REVISION, {},
        )


def test_artifact_path_schema_rejects_extra_fields():
    from tools.graph import schemas
    with pytest.raises(SchemaValidationError, match="unknown fields"):
        schemas.validate_payload(
            ARTIFACT_PATH_SET_ID, ARTIFACT_PATH_REVISION,
            {"path": "/p", "org": "anchore"},
        )


def test_peer_subscription_schema_accepts_empty_list():
    from tools.graph import schemas
    schemas.validate_payload(
        PEER_SUB_SET_ID, PEER_SUB_REVISION, {"peers": []},
    )  # no raise


def test_peer_subscription_schema_rejects_missing_peers():
    from tools.graph import schemas
    with pytest.raises(SchemaValidationError, match="'peers' is required"):
        schemas.validate_payload(PEER_SUB_SET_ID, PEER_SUB_REVISION, {})


def test_peer_subscription_schema_rejects_non_string_peer():
    from tools.graph import schemas
    with pytest.raises(SchemaValidationError, match="non-empty string"):
        schemas.validate_payload(
            PEER_SUB_SET_ID, PEER_SUB_REVISION, {"peers": [123]},
        )


# ── ops.read_set routed by org=personal ─────────────


def test_read_set_with_org_personal_returns_artifact_paths(
    synthetic_yaml, orgs_dir, monkeypatch,
):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)

    got = ops.read_set(ARTIFACT_PATH_SET_ID, org=PERSONAL_ORG_SLUG)
    keys = sorted(m.key for m in got.members)
    assert keys == [
        "anchore:id_ed25519",
        "anchore:license.yaml",
        "autonomy:custom-artifact",
    ]


def test_read_set_with_org_personal_returns_peer_subscriptions(
    synthetic_yaml, orgs_dir, monkeypatch,
):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)

    got = ops.read_set(PEER_SUB_SET_ID, org=PERSONAL_ORG_SLUG)
    by_key = {m.key: m.payload for m in got.members}
    assert by_key == {
        "autonomy": {"peers": ["personal"]},
        "personal": {"peers": []},
    }


# ── graph set members --org personal CLI ────────────


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


def test_cli_set_members_artifact_path_personal(
    synthetic_yaml, orgs_dir, monkeypatch,
):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)

    rc, out, err = _run_cli([
        "set", "members", ARTIFACT_PATH_SET_ID,
        "--org", PERSONAL_ORG_SLUG,
    ])
    assert rc == 0, err
    assert "anchore:license.yaml" in out
    assert "anchore:id_ed25519" in out
    assert "autonomy:custom-art" in out  # may be truncated


def test_cli_set_members_peer_subscription_personal(
    synthetic_yaml, orgs_dir, monkeypatch,
):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    plan = build_plan(synthetic_yaml, orgs_root=orgs_dir)
    apply_migration(plan)

    rc, out, err = _run_cli([
        "set", "members", PEER_SUB_SET_ID,
        "--org", PERSONAL_ORG_SLUG,
    ])
    assert rc == 0, err
    assert "autonomy" in out
    assert "personal" in out


def test_cli_set_members_empty_when_no_migration(orgs_dir, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    # Bootstrap personal.db without any operator-local Settings.
    from tools.graph import org_ops
    org_ops.ensure_bootstrap_orgs(root=orgs_dir)

    rc, out, err = _run_cli([
        "set", "members", ARTIFACT_PATH_SET_ID,
        "--org", PERSONAL_ORG_SLUG,
    ])
    assert rc == 0, err
    assert "no Settings in" in out


# ── CLI main() entry point ─────────────────────────────────


def test_migrate_main_dry_run(synthetic_yaml, orgs_dir, capsys):
    rc = migrate_main([
        "--yaml", str(synthetic_yaml),
        "--orgs-dir", str(orgs_dir),
        "--dry-run",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "to insert, 0 already present" in captured.out
    # Nothing should have been written.
    if (orgs_dir / "personal.db").exists():
        rows = _settings_rows(orgs_dir / "personal.db")
        assert not any(
            r["set_id"] in (ARTIFACT_PATH_SET_ID, PEER_SUB_SET_ID)
            for r in rows
        )


def test_migrate_main_writes(synthetic_yaml, orgs_dir, capsys):
    rc = migrate_main([
        "--yaml", str(synthetic_yaml),
        "--orgs-dir", str(orgs_dir),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "5 to insert" in captured.out
    assert (orgs_dir / "personal.db").exists()


def test_migrate_main_empty_plan_no_op(empty_yaml, orgs_dir, capsys):
    rc = migrate_main([
        "--yaml", str(empty_yaml),
        "--orgs-dir", str(orgs_dir),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "nothing to migrate" in captured.out
    assert not (orgs_dir / "personal.db").exists()


def test_migrate_main_handles_missing_yaml(orgs_dir, tmp_path, capsys):
    missing = tmp_path / "not-here.yaml"
    rc = migrate_main([
        "--yaml", str(missing),
        "--orgs-dir", str(orgs_dir),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "nothing to migrate" in captured.out
