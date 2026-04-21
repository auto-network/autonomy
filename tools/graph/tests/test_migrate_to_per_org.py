"""Tests for the per-org DB data migration script (auto-9iq2s, txg5.2).

Spec: graph://7c296600-19b (per-org DB decision) + bead description.
The migration partitions a synthetic legacy ``graph.db`` containing
content across multiple projects (autonomy, anchore, personal) plus
NULL/orphaned rows, and asserts that the per-org DBs end up with the
right rows, FTS5 indices, and provenance edges.
"""

from __future__ import annotations

import json
import os
import sqlite3
import textwrap
from pathlib import Path

import pytest

from tools.graph.db import GraphDB, REPO_ROOT
from tools.graph.migrations.migrate_to_per_org import (
    AlreadyMigratedError,
    PreflightError,
    apply_migration,
    backup_legacy_db,
    build_plan,
    check_idempotency,
    check_services_stopped,
    enumerate_target_orgs,
    main as migrate_main,
    rename_legacy,
    verify_migration,
)


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    """Build a synthetic legacy graph.db with rows across 3 projects.

    Layout:
      autonomy: 2 sources (one note, one session) + thoughts/derivations,
                a comment, a version, an attachment, edges, mentions.
      anchore:  1 session source + thoughts.
      personal: 1 note source.
      orphan project 'enterprise-old': 1 source — should route to autonomy
                                       with a warning.
      NULL project: 1 musing — should route to autonomy.
    Plus tags (global vocab) and a thread (operator-local).
    """
    path = tmp_path / "data" / "graph.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = GraphDB(path)

    def _src(sid, project, type_="note", title="t", file_path=None):
        meta = {"tags": ["pitfall"]} if type_ == "note" else {}
        db.conn.execute(
            "INSERT INTO sources("
            "id, type, platform, project, title, url, file_path, "
            "metadata, created_at, ingested_at, last_activity_at, "
            "publication_state) "
            "VALUES(?, ?, 'local', ?, ?, NULL, ?, ?, "
            "'2026-04-20T12:00:00Z', '2026-04-20T12:00:00Z', "
            "'2026-04-20T12:00:00Z', 'raw')",
            (sid, type_, project, title, file_path or sid,
             json.dumps(meta)),
        )

    _src("auton-note-1", "autonomy", title="autonomy note 1",
         file_path="/notes/auton-note-1.md")
    _src("auton-sess-1", "autonomy", type_="session",
         title="autonomy session 1",
         file_path="/sess/auton-sess-1.jsonl")
    _src("anchore-sess-1", "anchore", type_="session",
         title="anchore session 1",
         file_path="/sess/anchore-sess-1.jsonl")
    _src("personal-note-1", "personal", title="personal note 1",
         file_path="/notes/personal-note-1.md")
    _src("orphan-1", "enterprise-old", type_="session",
         title="orphan session",
         file_path="/sess/orphan-1.jsonl")
    _src("nullproj-1", None, type_="musing",
         title="musing without project",
         file_path="/musings/nullproj-1.md")

    # Thoughts + derivations (FTS triggers fire on insert)
    db.conn.execute(
        "INSERT INTO thoughts(id, source_id, content, role, turn_number) "
        "VALUES(?, ?, ?, 'user', 1)",
        ("th-auton-1", "auton-sess-1", "autonomy thought about settings"),
    )
    db.conn.execute(
        "INSERT INTO derivations("
        "id, source_id, thought_id, content, model, turn_number) "
        "VALUES(?, ?, ?, ?, 'claude', 2)",
        ("dr-auton-1", "auton-sess-1", "th-auton-1",
         "autonomy reply about settings"),
    )
    db.conn.execute(
        "INSERT INTO thoughts(id, source_id, content, role, turn_number) "
        "VALUES(?, ?, ?, 'user', 1)",
        ("th-anchore-1", "anchore-sess-1",
         "anchore thought about enterprise"),
    )
    db.conn.execute(
        "INSERT INTO thoughts(id, source_id, content, role, turn_number) "
        "VALUES(?, ?, ?, 'user', 1)",
        ("th-orphan-1", "orphan-1", "orphan thought"),
    )

    # An entity + mentions (one referenced from autonomy, one from anchore)
    db.conn.execute(
        "INSERT INTO entities(id, name, canonical_name, type) "
        "VALUES('e1', 'Autonomy Core', 'autonomy core', 'concept')"
    )
    db.conn.execute(
        "INSERT INTO entities(id, name, canonical_name, type) "
        "VALUES('e2', 'Enterprise', 'enterprise', 'concept')"
    )
    db.conn.execute(
        "INSERT INTO entities(id, name, canonical_name, type) "
        "VALUES('e3', 'Unused entity', 'unused entity', 'concept')"
    )
    db.conn.execute(
        "INSERT INTO entity_mentions(entity_id, content_id, content_type) "
        "VALUES('e1', 'th-auton-1', 'thought')"
    )
    db.conn.execute(
        "INSERT INTO entity_mentions(entity_id, content_id, content_type) "
        "VALUES('e2', 'th-anchore-1', 'thought')"
    )

    # Note comment + version + read on autonomy note
    db.conn.execute(
        "INSERT INTO note_comments(id, source_id, content) "
        "VALUES('c1', 'auton-note-1', 'good note')"
    )
    db.conn.execute(
        "INSERT INTO note_versions(source_id, version, content) "
        "VALUES('auton-note-1', 1, 'autonomy note v1')"
    )
    db.conn.execute(
        "INSERT INTO note_reads(source_id, actor) "
        "VALUES('auton-note-1', 'jeremy')"
    )

    # Attachment on anchore
    db.conn.execute(
        "INSERT INTO attachments("
        "id, hash, filename, mime_type, size_bytes, file_path, "
        "source_id) VALUES('att1', 'sha1', 'a.png', 'image/png', "
        "10, '/blob/sha1/a.png', 'anchore-sess-1')"
    )

    # An edge: autonomy session → autonomy note
    db.conn.execute(
        "INSERT INTO edges("
        "id, source_id, source_type, target_id, target_type, relation) "
        "VALUES('e-1', 'auton-sess-1', 'source', 'auton-note-1', "
        "'source', 'related_to')"
    )
    # Cross-org edge: anchore session → autonomy note (lands in anchore)
    db.conn.execute(
        "INSERT INTO edges("
        "id, source_id, source_type, target_id, target_type, relation) "
        "VALUES('e-2', 'anchore-sess-1', 'source', 'auton-note-1', "
        "'source', 'mentions')"
    )

    # Capture (with source) + capture (without source → personal)
    db.conn.execute(
        "INSERT INTO captures(id, content, source_id) "
        "VALUES('cap1', 'autonomy capture', 'auton-sess-1')"
    )
    db.conn.execute(
        "INSERT INTO captures(id, content) "
        "VALUES('cap2', 'orphan capture')"
    )

    # Threads (operator-local) and tags (global vocab)
    db.conn.execute(
        "INSERT INTO threads(id, title) VALUES('thr1', 'a thread')"
    )
    db.conn.execute(
        "INSERT INTO tags(name, description) "
        "VALUES('pitfall', 'operational hazards')"
    )
    db.conn.execute(
        "INSERT INTO tags(name, description) "
        "VALUES('signpost', 'architectural anchor')"
    )

    # A node (hierarchy) and node_ref → autonomy
    db.conn.execute(
        "INSERT INTO nodes(id, type, title) VALUES('n1', 'feature', 'F')"
    )
    db.conn.execute(
        "INSERT INTO node_refs(node_id, ref_id, ref_type) "
        "VALUES('n1', 'auton-note-1', 'source')"
    )

    # A claim (NULL source → autonomy)
    db.conn.execute(
        "INSERT INTO claims(id, subject_id, predicate, source_id) "
        "VALUES('cl1', 'e1', 'is_a', NULL)"
    )

    db.conn.commit()
    db.close()
    return path


@pytest.fixture
def orgs_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "data" / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    return root


@pytest.fixture
def yaml_path(tmp_path) -> Path:
    """Minimal yaml with autonomy + anchore graph_project values."""
    p = tmp_path / "projects.yaml"
    p.write_text(textwrap.dedent("""
        projects:
          autonomy:
            graph_project: autonomy
          enterprise:
            graph_project: anchore
          enterprise-ng:
            graph_project: anchore
    """).strip() + "\n")
    return p


# ── enumerate_target_orgs ───────────────────────────────────


def test_enumerate_baseline_only(tmp_path):
    slugs = enumerate_target_orgs(tmp_path / "missing.yaml")
    assert "autonomy" in slugs and "personal" in slugs


def test_enumerate_pulls_yaml_graph_projects(yaml_path):
    slugs = enumerate_target_orgs(yaml_path)
    assert set(["autonomy", "anchore", "personal"]).issubset(set(slugs))


def test_enumerate_handles_extras(yaml_path):
    slugs = enumerate_target_orgs(yaml_path, extra={"enterprise-old"})
    assert "enterprise-old" in slugs


# ── build_plan ──────────────────────────────────────────────


def test_plan_routes_known_projects(legacy_db, orgs_dir, yaml_path):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    assert "autonomy" in plan.org_slugs
    assert "anchore" in plan.org_slugs
    assert "personal" in plan.org_slugs
    # Autonomy: 2 own + 1 NULL-project + 1 orphan-project = 4.
    assert plan.partitions["autonomy"].sources == 4
    assert plan.partitions["anchore"].sources == 1
    assert plan.partitions["personal"].sources == 1


def test_plan_orphans_route_to_autonomy_with_warning(
    legacy_db, orgs_dir, yaml_path,
):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    # Two sources land in autonomy via fallback: NULL project + orphan.
    assert plan.unknown_project_count == 1
    assert "enterprise-old" in plan.unknown_projects
    assert plan.null_project_count == 1
    # Autonomy gets its own 2 + the 2 fallbacks = 4.
    assert plan.partitions["autonomy"].sources == 4


def test_plan_counts_per_source_children(legacy_db, orgs_dir, yaml_path):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    # Thoughts: 1 anchore + 1 autonomy + 1 orphan (→ autonomy).
    assert plan.partitions["autonomy"].thoughts == 2
    assert plan.partitions["anchore"].thoughts == 1
    # Edges: autonomy edge + cross-org edge counted under source endpoint.
    assert plan.partitions["autonomy"].edges == 1
    assert plan.partitions["anchore"].edges == 1


def test_plan_tags_duplicated_to_every_org(legacy_db, orgs_dir, yaml_path):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    for slug in plan.org_slugs:
        assert plan.partitions[slug].tags == 2


def test_plan_threads_route_to_autonomy(legacy_db, orgs_dir, yaml_path):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    assert plan.partitions["autonomy"].threads == 1
    assert plan.partitions["anchore"].threads == 0
    assert plan.partitions["personal"].threads == 0


# ── apply_migration ─────────────────────────────────────────


def test_apply_creates_per_org_dbs(legacy_db, orgs_dir, yaml_path):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    summary = apply_migration(plan, log=lambda *a, **kw: None)
    for slug in ("autonomy", "anchore", "personal"):
        assert (orgs_dir / f"{slug}.db").exists()
    assert summary["autonomy"] == 4
    assert summary["anchore"] == 1
    assert summary["personal"] == 1


def test_apply_partitions_thoughts_correctly(
    legacy_db, orgs_dir, yaml_path,
):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    auton = sqlite3.connect(orgs_dir / "autonomy.db")
    try:
        ids = {
            r[0] for r in auton.execute(
                "SELECT id FROM thoughts"
            ).fetchall()
        }
    finally:
        auton.close()
    assert ids == {"th-auton-1", "th-orphan-1"}


def test_apply_fts_index_populated(legacy_db, orgs_dir, yaml_path):
    """Inserting thoughts fires FTS triggers — search should hit."""
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    auton = sqlite3.connect(orgs_dir / "autonomy.db")
    auton.row_factory = sqlite3.Row
    try:
        rows = auton.execute(
            "SELECT rowid FROM thoughts_fts WHERE thoughts_fts MATCH ?",
            ("settings",),
        ).fetchall()
    finally:
        auton.close()
    assert len(rows) >= 1


def test_apply_attachments_follow_source(legacy_db, orgs_dir, yaml_path):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    anchore = sqlite3.connect(orgs_dir / "anchore.db")
    try:
        n = anchore.execute(
            "SELECT COUNT(*) FROM attachments"
        ).fetchone()[0]
    finally:
        anchore.close()
    assert n == 1
    auton = sqlite3.connect(orgs_dir / "autonomy.db")
    try:
        n2 = auton.execute(
            "SELECT COUNT(*) FROM attachments"
        ).fetchone()[0]
    finally:
        auton.close()
    assert n2 == 0


def test_apply_entities_replicated_only_where_referenced(
    legacy_db, orgs_dir, yaml_path,
):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    auton = sqlite3.connect(orgs_dir / "autonomy.db")
    try:
        autonomy_ent_names = {
            r[0] for r in auton.execute(
                "SELECT canonical_name FROM entities"
            ).fetchall()
        }
    finally:
        auton.close()
    anchore = sqlite3.connect(orgs_dir / "anchore.db")
    try:
        anchore_ent_names = {
            r[0] for r in anchore.execute(
                "SELECT canonical_name FROM entities"
            ).fetchall()
        }
    finally:
        anchore.close()
    # Autonomy mentions entity 'autonomy core' (e1)
    assert "autonomy core" in autonomy_ent_names
    # Anchore mentions entity 'enterprise' (e2)
    assert "enterprise" in anchore_ent_names
    # Unreferenced entity ('unused entity') gets pruned
    assert "unused entity" not in autonomy_ent_names
    assert "unused entity" not in anchore_ent_names


def test_apply_tags_in_every_org(legacy_db, orgs_dir, yaml_path):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    for slug in ("autonomy", "anchore", "personal"):
        conn = sqlite3.connect(orgs_dir / f"{slug}.db")
        try:
            n = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        finally:
            conn.close()
        assert n == 2, f"{slug} missing tags"


def test_apply_captures_unsourced_route_to_personal(
    legacy_db, orgs_dir, yaml_path,
):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    personal = sqlite3.connect(orgs_dir / "personal.db")
    try:
        ids = {
            r[0] for r in personal.execute(
                "SELECT id FROM captures"
            ).fetchall()
        }
    finally:
        personal.close()
    assert "cap2" in ids


# ── verify ──────────────────────────────────────────────────


def test_verify_passes_after_clean_migration(
    legacy_db, orgs_dir, yaml_path,
):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    report = verify_migration(plan)
    assert report.ok, report.issues


def test_verify_detects_partial_state(
    legacy_db, orgs_dir, yaml_path,
):
    """If a per-org DB is missing rows the verifier flags it."""
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    # Corrupt: drop one source from anchore so totals diverge.
    conn = sqlite3.connect(orgs_dir / "anchore.db")
    conn.execute("DELETE FROM sources WHERE id = 'anchore-sess-1'")
    conn.commit()
    conn.close()
    report = verify_migration(plan)
    assert not report.ok
    assert any("sources" in issue for issue in report.issues)


# ── pre-flight ──────────────────────────────────────────────


def test_check_services_stopped_no_pids(tmp_path):
    # Empty pid_dir: passes silently.
    check_services_stopped(tmp_path)


def test_check_services_stopped_dead_pid_passes(tmp_path):
    (tmp_path / "dashboard.pid").write_text("99999999\n")
    # Very high pid is unlikely to be alive.
    check_services_stopped(tmp_path)


def test_check_services_stopped_live_pid_refuses(tmp_path):
    (tmp_path / "dispatcher.pid").write_text(str(os.getpid()))
    with pytest.raises(PreflightError):
        check_services_stopped(tmp_path)


def test_backup_writes_copy(legacy_db):
    out = backup_legacy_db(legacy_db, timestamp="testts")
    assert out
    assert out[0].name.endswith(".pre-txg5-testts")
    assert out[0].exists()
    # Content matches.
    assert out[0].stat().st_size == legacy_db.stat().st_size


def test_backup_refuses_missing_legacy(tmp_path):
    with pytest.raises(PreflightError):
        backup_legacy_db(tmp_path / "nope.db", timestamp="x")


# ── idempotency ─────────────────────────────────────────────


def test_idempotency_blocks_when_both_signals_present(
    legacy_db, orgs_dir, yaml_path,
):
    plan = build_plan(legacy_db, orgs_dir, yaml_path)
    apply_migration(plan, log=lambda *a, **kw: None)
    rename_legacy(legacy_db, timestamp="testts")
    # Now both signals are present — the next run should refuse.
    new_legacy = legacy_db.parent / f"{legacy_db.name}.legacy-testts"
    assert new_legacy.exists()
    # Note: legacy_db (the original path) no longer exists, but the
    # idempotency check looks at the legacy-* sibling.
    with pytest.raises(AlreadyMigratedError):
        check_idempotency(orgs_dir, legacy_db)


def test_idempotency_allows_partial_state(legacy_db, orgs_dir):
    # No legacy-* backup yet → idempotency check passes.
    check_idempotency(orgs_dir, legacy_db)


# ── post-migration rename ───────────────────────────────────


def test_rename_legacy_moves_file(legacy_db):
    new_path = rename_legacy(legacy_db, timestamp="testts")
    assert not legacy_db.exists()
    assert new_path.exists()
    assert new_path.name == f"{legacy_db.name}.legacy-testts"


# ── end-to-end via main() ───────────────────────────────────


def test_main_dry_run_creates_no_dbs(
    legacy_db, orgs_dir, yaml_path, capsys,
):
    rc = migrate_main([
        "--legacy-db", str(legacy_db),
        "--orgs-dir", str(orgs_dir),
        "--projects-yaml", str(yaml_path),
        "--pid-dir", str(legacy_db.parent),
        "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Migration plan" in out
    assert "autonomy" in out
    # No per-org DBs created in dry-run.
    assert not orgs_dir.exists() or not list(orgs_dir.glob("*.db"))


def test_main_full_migration_succeeds(
    legacy_db, orgs_dir, yaml_path, capsys,
):
    rc = migrate_main([
        "--legacy-db", str(legacy_db),
        "--orgs-dir", str(orgs_dir),
        "--projects-yaml", str(yaml_path),
        "--pid-dir", str(legacy_db.parent),
    ])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Verification OK" in out
    assert (orgs_dir / "autonomy.db").exists()
    assert (orgs_dir / "anchore.db").exists()
    assert (orgs_dir / "personal.db").exists()
    # Legacy renamed.
    assert not legacy_db.exists()
    assert any(
        legacy_db.parent.glob(f"{legacy_db.name}.legacy-*")
    )


def test_main_refuses_when_dispatcher_running(
    legacy_db, orgs_dir, yaml_path, capsys,
):
    pid_dir = legacy_db.parent
    (pid_dir / "dispatcher.pid").write_text(str(os.getpid()))
    rc = migrate_main([
        "--legacy-db", str(legacy_db),
        "--orgs-dir", str(orgs_dir),
        "--projects-yaml", str(yaml_path),
        "--pid-dir", str(pid_dir),
    ])
    err = capsys.readouterr().err
    assert rc == 4
    assert "PREFLIGHT FAILED" in err


def test_main_refuses_when_already_migrated(
    legacy_db, orgs_dir, yaml_path, capsys,
):
    rc1 = migrate_main([
        "--legacy-db", str(legacy_db),
        "--orgs-dir", str(orgs_dir),
        "--projects-yaml", str(yaml_path),
        "--pid-dir", str(legacy_db.parent),
    ])
    assert rc1 == 0
    # Re-running on the renamed legacy should refuse politely.
    legacy_renamed = next(
        legacy_db.parent.glob(f"{legacy_db.name}.legacy-*")
    )
    # Restore a 'legacy_db' file so the re-run has something to look at,
    # but the idempotency signal (legacy-* + autonomy.db with rows)
    # remains true.
    legacy_db.touch()
    rc2 = migrate_main([
        "--legacy-db", str(legacy_db),
        "--orgs-dir", str(orgs_dir),
        "--projects-yaml", str(yaml_path),
        "--pid-dir", str(legacy_db.parent),
    ])
    err = capsys.readouterr().err
    assert rc2 == 3
    assert "ALREADY MIGRATED" in err
    assert legacy_renamed.exists()
