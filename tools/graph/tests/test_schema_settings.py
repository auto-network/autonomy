"""Schema migration tests for the settings primitive.

Verifies the table + indices land on fresh DBs, are idempotent on reopen,
and have no impact on adjacent tables (Settings is a NEW primitive, not a
Notes refactor). Spec: graph://0d3f750f-f9c.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source


@pytest.fixture
def fresh_db(tmp_path):
    db = GraphDB(tmp_path / "graph.db")
    yield db
    db.close()


def test_fresh_db_has_settings_table(fresh_db):
    row = fresh_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone()
    assert row is not None


def test_settings_columns_match_spec(fresh_db):
    cols = {r[1] for r in fresh_db.conn.execute("PRAGMA table_info(settings)").fetchall()}
    expected = {
        "id", "set_id", "schema_revision", "key", "payload",
        "publication_state", "supersedes", "excludes",
        "deprecated", "successor_id", "created_at", "updated_at",
    }
    assert expected <= cols


def test_settings_indices_present(fresh_db):
    rows = fresh_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='settings'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_settings_set" in names
    assert "idx_settings_state" in names
    assert "idx_settings_schema" in names


def test_publication_state_check_constraint(fresh_db):
    """Invalid publication_state must be rejected by the CHECK."""
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, publication_state, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("s1", "x.y", 1, "k", "{}", "garbage", "2026-01-01", "2026-01-01"),
        )


def test_migration_is_idempotent(tmp_path):
    """Reopening the DB should not error or duplicate anything."""
    p = tmp_path / "graph.db"
    db = GraphDB(p)
    db.close()
    db = GraphDB(p)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(settings)").fetchall()}
    db.close()
    assert "set_id" in cols


def test_existing_data_unaffected(tmp_path):
    """Inserting a Source into a DB with the new table works exactly as before."""
    p = tmp_path / "graph.db"
    db = GraphDB(p)
    src = Source(
        type="note", platform="local", project="autonomy",
        title="probe", file_path="note:probe",
        metadata={"tags": []},
    )
    db.insert_source(src)
    got = db.get_source(src.id)
    db.close()
    assert got is not None
    assert got["title"] == "probe"


def test_settings_round_trip(fresh_db):
    """A bare-metal INSERT/SELECT exercises the storage layer end-to-end."""
    fresh_db.conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("abc", "autonomy.test", 1, "foo", '{"x": 1}', "raw",
         "2026-04-21T00:00:00Z", "2026-04-21T00:00:00Z"),
    )
    fresh_db.conn.commit()
    row = fresh_db.conn.execute(
        "SELECT * FROM settings WHERE id = 'abc'"
    ).fetchone()
    assert row["set_id"] == "autonomy.test"
    assert row["schema_revision"] == 1
    assert row["key"] == "foo"
    assert row["payload"] == '{"x": 1}'
