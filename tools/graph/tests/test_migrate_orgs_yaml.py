"""Tests for the orgs yaml → autonomy.org#1 migration (auto-4a1cn).

Spec: graph://0d3f750f-f9c (Setting Primitive) + graph://d970d946-f95
(Org Registry). Covers:

* Synthetic yaml round-trips into the right per-org DB with the right
  payload.
* Idempotence — second run is a no-op.
* Missing org DB → auto-created (unlike the workspace migration).
* Type inference ('personal' slug → personal, else shared).
* Payload validates against the registered schema.
* Re-running against a DB bootstrapped with autonomy.org#1 already
  seeded (e.g. by ``ensure_bootstrap_orgs``) is a no-op.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tools.graph import org_ops, schemas
from tools.graph.db import GraphDB
from tools.graph.migrations.migrate_orgs_yaml import (
    OrgMigrationError,
    apply_migration,
    build_plan,
    main as migrate_main,
)
from tools.graph.schemas.org import ORG_SET_ID, ORG_REVISION


YAML_FIXTURE = textwrap.dedent(
    """
    orgs:
      autonomy:
        name: "Autonomy Network"
        byline: "AGI platform"
        color: "#6C63FF"
        favicon: /static/icon-192.png
      anchore:
        name: "Anchore"
        byline: "Security platform"
        color: "#2D7DD2"
        favicon: /static/orgs/anchore.png
      personal:
        name: "Personal"
        color: "#A0A0A0"

    projects: {}
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
    """Empty per-org DB root. Migration will create DBs itself."""
    d = tmp_path / "orgs"
    d.mkdir()
    return d


@pytest.fixture
def prebootstrapped_orgs_dir(tmp_path, monkeypatch) -> Path:
    """Orgs dir pre-seeded by ``ensure_bootstrap_orgs`` (autonomy + personal)."""
    d = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(d))
    org_ops.ensure_bootstrap_orgs(root=d)
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


def test_migration_creates_per_org_dbs(yaml_path, orgs_dir):
    apply_migration(build_plan(yaml_path, orgs_dir))

    for slug in ("autonomy", "anchore", "personal"):
        assert (orgs_dir / f"{slug}.db").exists(), (
            f"expected {slug}.db to be created"
        )


def test_migration_payload_shape(yaml_path, orgs_dir):
    apply_migration(build_plan(yaml_path, orgs_dir))

    rows = _settings_rows(orgs_dir / "autonomy.db")
    by_key = {r["key"]: r for r in rows if r["set_id"] == ORG_SET_ID}
    aut = by_key["autonomy"]
    assert aut["schema_revision"] == ORG_REVISION
    assert aut["publication_state"] == "canonical"
    assert aut["payload"] == {
        "name": "Autonomy Network",
        "byline": "AGI platform",
        "color": "#6C63FF",
        "favicon": "/static/icon-192.png",
        "type": "shared",
    }

    anc = {
        r["key"]: r for r in _settings_rows(orgs_dir / "anchore.db")
        if r["set_id"] == ORG_SET_ID
    }["anchore"]
    assert anc["payload"]["type"] == "shared"
    assert anc["payload"]["name"] == "Anchore"
    assert anc["payload"]["byline"] == "Security platform"

    per = {
        r["key"]: r for r in _settings_rows(orgs_dir / "personal.db")
        if r["set_id"] == ORG_SET_ID
    }["personal"]
    assert per["payload"] == {
        "name": "Personal",
        "color": "#A0A0A0",
        "type": "personal",
    }


def test_migrated_payload_validates_against_schema(yaml_path, orgs_dir):
    apply_migration(build_plan(yaml_path, orgs_dir))
    for slug in ("autonomy", "anchore", "personal"):
        for row in _settings_rows(orgs_dir / f"{slug}.db"):
            if row["set_id"] != ORG_SET_ID:
                continue
            schemas.validate_payload(
                row["set_id"], row["schema_revision"], row["payload"],
            )  # no raise


def test_migration_one_setting_per_org(yaml_path, orgs_dir):
    """Acceptance §1: one autonomy.org#1 Setting per org."""
    apply_migration(build_plan(yaml_path, orgs_dir))
    for slug in ("autonomy", "anchore", "personal"):
        rows = [
            r for r in _settings_rows(orgs_dir / f"{slug}.db")
            if r["set_id"] == ORG_SET_ID
        ]
        assert len(rows) == 1
        assert rows[0]["key"] == slug


def test_migration_is_idempotent(yaml_path, orgs_dir):
    apply_migration(build_plan(yaml_path, orgs_dir))
    before = {
        slug: [
            r["id"] for r in _settings_rows(orgs_dir / f"{slug}.db")
            if r["set_id"] == ORG_SET_ID
        ]
        for slug in ("autonomy", "anchore", "personal")
    }
    # Second run → no-op.
    report2 = build_plan(yaml_path, orgs_dir)
    for entry in report2.entries:
        assert entry.action == "skip_exists", (
            f"{entry.slug} not skip_exists: {entry.action}"
        )
    apply_migration(report2)
    after = {
        slug: [
            r["id"] for r in _settings_rows(orgs_dir / f"{slug}.db")
            if r["set_id"] == ORG_SET_ID
        ]
        for slug in ("autonomy", "anchore", "personal")
    }
    assert before == after


def test_migration_is_noop_after_ensure_bootstrap(
    yaml_path, prebootstrapped_orgs_dir,
):
    """Re-running over already-bootstrapped DBs must not duplicate Settings."""
    orgs_dir = prebootstrapped_orgs_dir

    before_aut = [
        r for r in _settings_rows(orgs_dir / "autonomy.db")
        if r["set_id"] == ORG_SET_ID
    ]
    # ensure_bootstrap_orgs should have seeded autonomy + personal when
    # the schema is registered (which it is in this bead).
    assert len(before_aut) == 1

    report = build_plan(yaml_path, orgs_dir)
    # autonomy + personal skip; anchore DB doesn't exist yet.
    by_slug = {e.slug: e.action for e in report.entries}
    assert by_slug["autonomy"] == "skip_exists"
    assert by_slug["personal"] == "skip_exists"
    assert by_slug["anchore"] == "create_db_and_insert"

    apply_migration(report)

    after_aut = [
        r for r in _settings_rows(orgs_dir / "autonomy.db")
        if r["set_id"] == ORG_SET_ID
    ]
    assert len(after_aut) == 1
    # Original row preserved — bootstrap's seed, not the migration's.
    assert after_aut[0]["id"] == before_aut[0]["id"]

    # Anchore DB created with identity.
    anc_rows = [
        r for r in _settings_rows(orgs_dir / "anchore.db")
        if r["set_id"] == ORG_SET_ID
    ]
    assert len(anc_rows) == 1
    assert anc_rows[0]["key"] == "anchore"


# ── Type inference ────────────────────────────────────────


def test_type_inference_from_bootstrap_row(tmp_path, yaml_path):
    """When the org DB pre-exists with a given type, migration honours it."""
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    # Intentional off-convention: create 'personal' slug as shared type
    # to verify the migration trusts the bootstrap row.
    GraphDB.create_org_db(
        "personal", type_="shared", path=orgs_dir / "personal.db",
    ).close()

    report = build_plan(yaml_path, orgs_dir)
    by_slug = {e.slug: e for e in report.entries}
    assert by_slug["personal"].org_type == "shared"
    assert by_slug["personal"].payload["type"] == "shared"


def test_type_inference_defaults_by_slug(yaml_path, orgs_dir):
    """No pre-existing DBs: 'personal' → personal, others → shared."""
    report = build_plan(yaml_path, orgs_dir)
    by_slug = {e.slug: e for e in report.entries}
    assert by_slug["autonomy"].org_type == "shared"
    assert by_slug["anchore"].org_type == "shared"
    assert by_slug["personal"].org_type == "personal"


# ── Yaml edge cases ───────────────────────────────────────


def test_empty_orgs_block_is_ok(tmp_path, orgs_dir):
    p = tmp_path / "empty.yaml"
    p.write_text("orgs: {}\nprojects: {}\n")
    report = build_plan(p, orgs_dir)
    assert report.entries == []
    apply_migration(report)  # no raise
    assert list(orgs_dir.glob("*.db")) == []


def test_missing_orgs_block_is_ok(tmp_path, orgs_dir):
    """``orgs:`` section absent → nothing to migrate."""
    p = tmp_path / "only-projects.yaml"
    p.write_text("projects: {}\n")
    report = build_plan(p, orgs_dir)
    assert report.entries == []


def test_null_entry_uses_slug_as_name(tmp_path, orgs_dir):
    """``orgs: {foo: null}`` is valid shorthand — name defaults to slug."""
    p = tmp_path / "shorthand.yaml"
    p.write_text("orgs:\n  bar:\n")
    report = build_plan(p, orgs_dir)
    by_slug = {e.slug: e for e in report.entries}
    assert by_slug["bar"].payload["name"] == "bar"


def test_bad_orgs_type_raises(tmp_path, orgs_dir):
    p = tmp_path / "bad.yaml"
    p.write_text("orgs: not-a-mapping\n")
    with pytest.raises(OrgMigrationError):
        build_plan(p, orgs_dir)


def test_bad_org_entry_raises(tmp_path, orgs_dir):
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent(
        """
        orgs:
          weird: "just a string"
        """
    ))
    with pytest.raises(OrgMigrationError):
        build_plan(p, orgs_dir)


# ── Cross-layer: settings_ops.read_set ────────────────────


def test_read_set_returns_org_identity_per_caller(
    yaml_path, orgs_dir, monkeypatch,
):
    """Acceptance §3: each org's ``autonomy.org#1`` Setting surfaces via
    ``read_set`` when that org is the caller."""
    apply_migration(build_plan(yaml_path, orgs_dir))

    from tools.graph import settings_ops

    def _with_caller(slug: str):
        monkeypatch.setenv("GRAPH_DB", str(orgs_dir / f"{slug}.db"))
        return settings_ops.read_set(ORG_SET_ID, org=slug)

    for slug in ("autonomy", "anchore", "personal"):
        result = _with_caller(slug)
        keys = sorted(m.key for m in result.members)
        assert keys == [slug], (
            f"{slug} DB should expose exactly its own identity; got {keys}"
        )


# ── CLI entrypoint ────────────────────────────────────────


def test_cli_dry_run(yaml_path, orgs_dir, capsys):
    rc = migrate_main([
        "--projects-yaml", str(yaml_path),
        "--orgs-dir", str(orgs_dir),
        "--dry-run",
    ])
    assert rc == 0
    # No DBs created.
    assert list(orgs_dir.glob("*.db")) == []


def test_cli_apply(yaml_path, orgs_dir, capsys):
    rc = migrate_main([
        "--projects-yaml", str(yaml_path),
        "--orgs-dir", str(orgs_dir),
    ])
    assert rc == 0
    assert {p.name for p in orgs_dir.glob("*.db")} == {
        "autonomy.db", "anchore.db", "personal.db",
    }
    for slug in ("autonomy", "anchore", "personal"):
        rows = [
            r for r in _settings_rows(orgs_dir / f"{slug}.db")
            if r["set_id"] == ORG_SET_ID
        ]
        assert len(rows) == 1
        assert rows[0]["key"] == slug


def test_real_projects_yaml_migrates_cleanly(tmp_path, monkeypatch):
    """Acceptance §3 sanity: the frozen shipped yaml fixture maps
    cleanly — autonomy, anchore, personal land with canonical identity
    in their respective DBs.

    ``agents/projects.yaml`` itself was retired in auto-gko4e; the
    fixture under ``agents/tests/fixtures/`` preserves its final shape
    for regression coverage.
    """
    from agents.tests.conftest import SHIPPED_PROJECTS_YAML

    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    report = build_plan(SHIPPED_PROJECTS_YAML, orgs_dir)
    apply_migration(report)

    slugs = {e.slug for e in report.entries}
    assert {"autonomy", "anchore", "personal"}.issubset(slugs)
    for slug in ("autonomy", "anchore", "personal"):
        rows = [
            r for r in _settings_rows(orgs_dir / f"{slug}.db")
            if r["set_id"] == ORG_SET_ID
        ]
        assert len(rows) == 1
        payload = rows[0]["payload"]
        assert payload["name"]  # non-empty
        assert payload["type"] in ("shared", "personal")
