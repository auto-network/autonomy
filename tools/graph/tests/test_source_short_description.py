"""Tests for the ``sources.short_description`` column + write-path plumbing.

Covers the bead auto-kt3z3 contract:
- migration adds the column on legacy DBs (idempotent),
- ``ops.create_note`` persists ``short_description`` when supplied,
- a leading markdown ``# heading`` is stripped from the stored ``title``
  while the body content is left intact, and
- ``ops.update_note(..., short_description="…")`` overwrites the column.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph import ops
from tools.graph.db import GraphDB


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    return db_path


def _column_names(db_path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
    finally:
        conn.close()


def test_migration_adds_short_description_column_idempotent(tmp_path):
    """Opening a fresh DB twice still leaves exactly one short_description col."""
    db_path = tmp_path / "g.db"

    # First open creates the schema (including the new migration).
    db = GraphDB(str(db_path))
    db.close()
    cols_first = _column_names(db_path)
    assert "short_description" in cols_first

    # Second open is a no-op for the migration — it must not fail or duplicate.
    db = GraphDB(str(db_path))
    db.close()
    cols_second = _column_names(db_path)
    assert cols_second == cols_first


def test_migration_idempotent_on_pre_existing_legacy_db(tmp_path):
    """Simulate a legacy DB with sources but no short_description column.

    The migration must add the column without disturbing existing rows.
    """
    db_path = tmp_path / "legacy.db"

    # Bootstrap a full real schema, insert a row, then drop the new column to
    # simulate a DB that pre-dates this migration. SQLite ≥3.35 supports
    # ALTER TABLE … DROP COLUMN, so this stays self-contained.
    db = GraphDB(str(db_path))
    from tools.graph.models import Source
    db.insert_source(Source(
        id="legacy-1", type="note", title="legacy",
        file_path="note:legacy-1",
    ))
    db.close()

    conn = sqlite3.connect(str(db_path))
    # The sources_fts triggers (added in auto-kvka6) reference
    # new.short_description / old.short_description, so they must come
    # down before SQLite will let us DROP COLUMN.
    for trg in ("sources_ai", "sources_ad", "sources_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trg}")
    conn.execute("DROP TABLE IF EXISTS sources_fts")
    conn.execute("ALTER TABLE sources DROP COLUMN short_description")
    conn.commit()
    conn.close()

    cols_before = _column_names(db_path)
    assert "short_description" not in cols_before

    # Opening again must re-run the migration on the legacy shape.
    db = GraphDB(str(db_path))
    db.close()

    cols_after = _column_names(db_path)
    assert "short_description" in cols_after

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT short_description, title FROM sources WHERE id = 'legacy-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] is None  # backfilled to NULL, not auto-derived
    assert row[1] == "legacy"  # existing data untouched


def test_note_write_with_short_description_persists(graph_db_env):
    """create_note(short_description="…") round-trips through get_source."""
    result = ops.create_note(
        "body content here",
        short_description="A test note about X",
    )
    src = ops.get_source(result["source_id"])
    assert src is not None
    assert src["short_description"] == "A test note about X"
    # Returned dict surfaces the field too — frontends use it before refetching.
    assert result["short_description"] == "A test note about X"


def test_note_title_strips_leading_heading_marker(graph_db_env):
    """``# Topic\\n\\nbody`` → title='Topic'; content stays unchanged."""
    content = "# Topic\n\nbody text follows"
    result = ops.create_note(content)
    src = ops.get_source(result["source_id"])
    assert src is not None
    assert src["title"] == "Topic"
    # Body content is untouched — the heading marker stays in the note body.
    assert result["content"] == content
    # No short_description was supplied → column stays NULL.
    assert src["short_description"] is None


def test_note_title_no_heading_falls_back_to_first_chars(graph_db_env):
    """Non-heading content yields the legacy ``content[:80]`` title."""
    content = "plain note without a heading marker"
    result = ops.create_note(content)
    src = ops.get_source(result["source_id"])
    assert src is not None
    assert src["title"] == content[:80]


def test_note_update_can_set_short_description(graph_db_env):
    """update_note(..., short_description="Y") overwrites the column."""
    created = ops.create_note("# Initial\n\nbody", short_description="X")
    sid = created["source_id"]

    # Update with a new short_description.
    ops.update_note(sid, "# Initial\n\nbody (revised)", short_description="Y")

    src = ops.get_source(sid)
    assert src is not None
    assert src["short_description"] == "Y"


def test_note_update_short_description_none_preserves_existing(graph_db_env):
    """Passing short_description=None on update leaves the column untouched.

    The Round 5 backfill flow updates title+summary together, but pure body
    edits should keep whatever description was last set (typed or auto).
    """
    created = ops.create_note("body", short_description="keep me")
    sid = created["source_id"]

    ops.update_note(sid, "new body")

    src = ops.get_source(sid)
    assert src is not None
    assert src["short_description"] == "keep me"
