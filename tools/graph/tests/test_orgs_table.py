"""Schema-migration tests for the bootstrap ``orgs`` table.

Spec: graph://d970d946-f95. The migration is idempotent and runs on
every ``GraphDB`` open — newly-created DBs gain the table, existing
DBs without it gain it on the next open without losing data.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import GraphDB


def test_new_db_has_orgs_table(tmp_path):
    db_path = tmp_path / "fresh.db"
    db = GraphDB(db_path)
    try:
        cols = {
            r[1] for r in db.conn.execute(
                "PRAGMA table_info(orgs)"
            ).fetchall()
        }
    finally:
        db.close()
    assert {"id", "slug", "type", "created_at"} <= cols


def test_orgs_table_is_empty_on_create(tmp_path):
    db = GraphDB(tmp_path / "fresh.db")
    try:
        n = db.conn.execute("SELECT COUNT(*) FROM orgs").fetchone()[0]
    finally:
        db.close()
    assert n == 0


def test_existing_db_gains_orgs_table_on_reopen(tmp_path):
    """Simulate a legacy DB that pre-dates this bead: drop the table,
    close, reopen — the migration must restore it without touching data.
    """
    db_path = tmp_path / "legacy.db"
    db = GraphDB(db_path)
    db.conn.execute("DROP TABLE IF EXISTS orgs")
    db.conn.commit()
    # Insert a fixture row in another table so we can verify it survives.
    db.conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES('fixture','autonomy.test',1,'k','{}','raw','t','t')",
    )
    db.conn.commit()
    db.close()

    db2 = GraphDB(db_path)
    try:
        cols = {
            r[1] for r in db2.conn.execute(
                "PRAGMA table_info(orgs)"
            ).fetchall()
        }
        # Survives.
        n_settings = db2.conn.execute(
            "SELECT COUNT(*) FROM settings"
        ).fetchone()[0]
    finally:
        db2.close()
    assert {"id", "slug", "type", "created_at"} <= cols
    assert n_settings == 1


def test_orgs_table_type_check_constraint(tmp_path):
    """Bootstrap ``type`` column accepts only 'shared' or 'personal'."""
    db = GraphDB(tmp_path / "fresh.db")
    try:
        db.conn.execute(
            "INSERT INTO orgs(id, slug, type, created_at) "
            "VALUES('id1','x','shared','t')"
        )
        db.conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO orgs(id, slug, type, created_at) "
                "VALUES('id2','y','rogue-type','t')"
            )
            db.conn.commit()
    finally:
        db.close()


def test_orgs_migration_is_idempotent(tmp_path):
    """Reopening a DB many times must not fail or duplicate the table."""
    db_path = tmp_path / "x.db"
    for _ in range(3):
        db = GraphDB(db_path)
        db.close()
    db = GraphDB(db_path)
    try:
        n_tables = db.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='orgs'"
        ).fetchone()[0]
    finally:
        db.close()
    assert n_tables == 1
