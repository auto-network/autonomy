"""Tests for the per-org DB schema factory.

Spec: graph://d970d946-f95 (Org Registry) + graph://7c296600-19b (per-org DB
decision). ``GraphDB.create_org_db`` produces a per-org DB at
``data/orgs/<slug>.db`` with the canonical schema (every table, index,
FTS5 virtual table, plus the ``orgs`` bootstrap row, ``settings``, and
``publication_state`` columns) — identical in shape to today's legacy
``data/graph.db``.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import GraphDB, VALID_ORG_TYPES


# ── Helpers ──────────────────────────────────────────────────


def _schema_shape(conn: sqlite3.Connection) -> dict:
    """Return a comparable schema shape: tables, their columns, indices,
    triggers, virtual FTS tables. Excludes orgs/settings/publication_state
    additions so pre-migration and per-org DBs line up after accounting
    for the known deltas."""
    shape: dict = {"tables": {}, "indices": set(), "triggers": set()}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    for (name,) in rows:
        if name.startswith("sqlite_") or name.endswith("_data") \
                or name.endswith("_idx") or name.endswith("_content") \
                or name.endswith("_docsize") or name.endswith("_config"):
            continue
        cols = [
            r[1] for r in conn.execute(
                f"PRAGMA table_info({name})"
            ).fetchall()
        ]
        shape["tables"][name] = cols
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        shape["indices"].add(name)
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall():
        shape["triggers"].add(name)
    return shape


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _evict_pool():
    """Ensure each test starts and ends with an empty connection pool."""
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


# ── Schema factory ───────────────────────────────────────────


def test_create_org_db_produces_file_with_full_schema(orgs_root):
    db = GraphDB.create_org_db("anchore", type_="shared")
    try:
        assert db.db_path.exists()
        assert db.db_path == orgs_root / "anchore.db"
        shape = _schema_shape(db.conn)
    finally:
        db.close()
    # Canonical tables must all be present.
    for required in (
        "sources", "thoughts", "derivations", "entities", "claims",
        "edges", "entity_mentions", "nodes", "node_refs",
        "note_comments", "note_versions", "attachments", "note_reads",
        "tags", "threads", "captures",
        "orgs", "settings",
    ):
        assert required in shape["tables"], f"missing table: {required}"
    # publication_state column landed on sources.
    assert "publication_state" in shape["tables"]["sources"]
    # FTS virtual tables registered.
    assert "thoughts_fts" in shape["tables"]
    assert "derivations_fts" in shape["tables"]
    assert "captures_fts" in shape["tables"]


def test_create_org_db_seeds_orgs_row(orgs_root):
    db = GraphDB.create_org_db("anchore", type_="shared")
    try:
        rows = db.conn.execute(
            "SELECT id, slug, type, created_at FROM orgs"
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == "anchore"
    assert row["type"] == "shared"
    # UUID v7 shape (version nibble is the leading char of the 3rd group).
    parts = row["id"].split("-")
    assert len(parts) == 5
    assert parts[2][0] == "7"


def test_create_org_db_personal_type(orgs_root):
    db = GraphDB.create_org_db("personal", type_="personal")
    try:
        row = db.conn.execute(
            "SELECT type FROM orgs"
        ).fetchone()
    finally:
        db.close()
    assert row["type"] == "personal"


def test_create_org_db_rejects_invalid_type(orgs_root):
    with pytest.raises(ValueError):
        GraphDB.create_org_db("x", type_="bogus")


def test_create_org_db_refuses_existing_file(orgs_root):
    db = GraphDB.create_org_db("anchore")
    db.close()
    with pytest.raises(FileExistsError):
        GraphDB.create_org_db("anchore")


def test_create_org_db_accepts_explicit_path(tmp_path):
    target = tmp_path / "nested" / "custom.db"
    db = GraphDB.create_org_db("x", path=target)
    try:
        assert target.exists()
        assert db.db_path == target
        row = db.conn.execute(
            "SELECT slug FROM orgs"
        ).fetchone()
    finally:
        db.close()
    assert row["slug"] == "x"


def test_create_org_db_schema_matches_legacy_db(tmp_path, orgs_root):
    """Per-org DB and the legacy ``data/graph.db`` produce the same schema.

    Both run through ``GraphDB._init_schema`` so every table, index, FTS5
    virtual table and migration lands on both. The only differences
    allowed are the bootstrap ``orgs`` row count (0 for legacy, 1 for
    per-org) — the *schema* itself is identical.
    """
    legacy = GraphDB(tmp_path / "legacy.db")
    try:
        legacy_shape = _schema_shape(legacy.conn)
    finally:
        legacy.close()

    per_org = GraphDB.create_org_db("anchore")
    try:
        per_org_shape = _schema_shape(per_org.conn)
    finally:
        per_org.close()
    assert legacy_shape == per_org_shape


def test_create_org_db_sources_has_publication_state(orgs_root):
    db = GraphDB.create_org_db("a")
    try:
        # Confirm check constraint accepts curated/published/canonical.
        for state in ("raw", "curated", "published", "canonical"):
            db.conn.execute(
                "INSERT INTO sources(id, type, publication_state) "
                "VALUES(?, 'note', ?)", (f"sid-{state}", state),
            )
        db.conn.commit()
    finally:
        db.close()


def test_create_org_db_settings_table_ready(orgs_root):
    """Settings primitive operational — insert + select round-trips."""
    db = GraphDB.create_org_db("a")
    try:
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload) "
            "VALUES('s1', 'autonomy.test', 1, 'k', '{}')"
        )
        db.conn.commit()
        row = db.conn.execute(
            "SELECT payload, publication_state FROM settings WHERE id='s1'"
        ).fetchone()
    finally:
        db.close()
    assert row["payload"] == "{}"
    assert row["publication_state"] == "raw"  # default


def test_create_org_db_valid_types_exposed():
    """``VALID_ORG_TYPES`` is the canonical list the factory enforces."""
    assert set(VALID_ORG_TYPES) == {"shared", "personal"}


# ── open_org_db ──────────────────────────────────────────────


def test_open_org_db_opens_existing(orgs_root):
    GraphDB.create_org_db("x").close()
    db = GraphDB.open_org_db("x")
    try:
        row = db.conn.execute("SELECT slug FROM orgs").fetchone()
    finally:
        db.close()
    assert row["slug"] == "x"


def test_open_org_db_missing_raises(orgs_root):
    with pytest.raises(FileNotFoundError):
        GraphDB.open_org_db("ghost")


def test_open_org_db_ro_mode_reads_but_cannot_write(orgs_root):
    GraphDB.create_org_db("x").close()
    db = GraphDB.open_org_db("x", mode="ro")
    try:
        assert db.read_only is True
        with pytest.raises(sqlite3.OperationalError):
            db.conn.execute(
                "INSERT INTO orgs(id, slug, type, created_at) "
                "VALUES('x', 'y', 'shared', 't')"
            )
    finally:
        db.close()
