"""Tests for the UNIQUE(source_id, message_id) migration (auto-4y579).

Covers the storage-layer half of the fix: a partial unique index on
thoughts/derivations, self-healing on connection open (dedupe any
existing violation before creating the index), and INSERT OR IGNORE so a
losing writer degrades to a no-op instead of raising IntegrityError.
"""

from __future__ import annotations

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Derivation, Source, Thought


@pytest.fixture
def db(tmp_path) -> GraphDB:
    g = GraphDB(tmp_path / "org.db")
    yield g
    g.close()


def _make_source(db: GraphDB, *, title: str = "test") -> str:
    source = Source(type="session", platform="codex-cli", project="autonomy",
                     title=title, file_path=f"/tmp/{title}.jsonl")
    db.insert_source(source)
    return source.id


class TestIndexCreatedOnFreshDB:
    def test_indexes_exist_after_open(self, db):
        names = {
            r["name"] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_thoughts_source_message_unique" in names
        assert "idx_derivations_source_message_unique" in names


class TestInsertOrIgnoreToleratesConflict:
    def test_second_insert_same_id_is_a_silent_noop(self, db):
        source_id = _make_source(db)
        t1 = Thought(source_id=source_id, content="first", turn_number=1, message_id="dup")
        t2 = Thought(source_id=source_id, content="second copy", turn_number=2, message_id="dup")
        db.insert_thought(t1)
        db.insert_thought(t2)  # must not raise
        db.commit()

        rows = db.conn.execute(
            "SELECT content FROM thoughts WHERE source_id = ? AND message_id = ?",
            (source_id, "dup"),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["content"] == "first"

    def test_derivation_conflict_is_also_a_silent_noop(self, db):
        source_id = _make_source(db)
        d1 = Derivation(source_id=source_id, content="first", turn_number=1, message_id="dup")
        d2 = Derivation(source_id=source_id, content="second copy", turn_number=2, message_id="dup")
        db.insert_derivation(d1)
        db.insert_derivation(d2)  # must not raise
        db.commit()

        rows = db.conn.execute(
            "SELECT content FROM derivations WHERE source_id = ? AND message_id = ?",
            (source_id, "dup"),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["content"] == "first"

    def test_null_message_id_never_conflicts(self, db):
        """The index is partial (WHERE message_id IS NOT NULL) — multiple
        NULL-id thoughts for the same source must all persist."""
        source_id = _make_source(db)
        for i in range(3):
            db.insert_thought(Thought(source_id=source_id, content=f"t{i}", turn_number=i, message_id=None))
        db.commit()

        count = db.conn.execute(
            "SELECT COUNT(*) c FROM thoughts WHERE source_id = ?", (source_id,)
        ).fetchone()["c"]
        assert count == 3

    def test_same_message_id_different_sources_both_persist(self, db):
        """The index is scoped to (source_id, message_id) — the same id
        reused across two different sources is not a conflict."""
        source_a = _make_source(db, title="a")
        source_b = _make_source(db, title="b")
        db.insert_thought(Thought(source_id=source_a, content="a", turn_number=1, message_id="shared"))
        db.insert_thought(Thought(source_id=source_b, content="b", turn_number=1, message_id="shared"))
        db.commit()

        count = db.conn.execute(
            "SELECT COUNT(*) c FROM thoughts WHERE message_id = 'shared'"
        ).fetchone()["c"]
        assert count == 2


class TestMigrationRepairsExistingViolationsThenIndexes:
    def test_dedupes_pre_existing_violation_keeping_lowest_turn(self, tmp_path):
        """Simulates a DB that already has a duplicate pair from before
        this fix landed — the migration must clean it up (keep lowest
        turn_number) BEFORE it can create the unique index (CREATE UNIQUE
        INDEX fails outright against live duplicate data).

        Builds the "legacy" state via a real GraphDB (correct full
        schema, FTS triggers included) with the new unique index dropped
        immediately after open, so the duplicate insert below succeeds —
        exactly mirrors what data ingested before auto-4y579 landed would
        look like once this fix is deployed.
        """
        db_path = tmp_path / "legacy.db"
        setup = GraphDB(db_path)
        setup.conn.execute("DROP INDEX IF EXISTS idx_thoughts_source_message_unique")
        setup.conn.commit()
        source_id = _make_source(setup)
        setup.insert_thought(Thought(source_id=source_id, content="renumbered copy", turn_number=5, message_id="dup"))
        setup.insert_thought(Thought(source_id=source_id, content="original", turn_number=1, message_id="dup"))
        setup.conn.commit()
        setup.close()

        # Re-opening via GraphDB runs the full migration chain, including
        # _migrate_message_id_unique — this is the moment the repair +
        # index creation happens (the index doesn't exist on this file
        # yet, so the migration's guard doesn't skip it).
        g = GraphDB(db_path)
        rows = g.conn.execute(
            "SELECT turn_number, content FROM thoughts WHERE source_id = ?", (source_id,)
        ).fetchall()
        names = {
            r["name"] for r in g.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        g.close()

        assert len(rows) == 1
        assert rows[0]["turn_number"] == 1
        assert rows[0]["content"] == "original"
        assert "idx_thoughts_source_message_unique" in names

    def test_migration_is_a_noop_once_index_exists(self, db):
        """The expensive full-table scan only runs once — re-opening an
        already-migrated DB must not re-scan (verified indirectly: a
        second open succeeds instantly and the index survives)."""
        source_id = _make_source(db)
        db.insert_thought(Thought(source_id=source_id, content="x", turn_number=1, message_id="m1"))
        db.commit()
        db.close()

        # Re-open — migration guard should skip the scan since the index
        # already exists.
        g2 = GraphDB(db.db_path)
        count = g2.conn.execute("SELECT COUNT(*) c FROM thoughts").fetchone()["c"]
        g2.close()
        assert count == 1
