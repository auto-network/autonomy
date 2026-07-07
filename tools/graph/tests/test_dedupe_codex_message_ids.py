"""Tests for the auto-cpg1x repair tool (audit + surgical dedup of
codex sources damaged by the pre-fix positional-cursor duplication bug).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.graph.checks.dedupe_codex_message_ids import (
    audit_db,
    find_duplicate_groups,
    repair_db,
)
from tools.graph.db import GraphDB
from tools.graph.models import Derivation, Source, Thought


@pytest.fixture
def db(tmp_path) -> GraphDB:
    """auto-4y579 added a UNIQUE(source_id, message_id) partial index —
    a real GraphDB now refuses to persist the very duplicates this test
    file exists to exercise repairing. Drop it after creation so these
    tests can still construct pre-existing-violation fixtures (exactly
    the "damage from before this fix landed" scenario the repair tool
    targets); the constraint's own correctness is covered separately in
    tools/graph/tests/test_message_id_unique_migration.py."""
    g = GraphDB(tmp_path / "org.db")
    g.conn.execute("DROP INDEX IF EXISTS idx_thoughts_source_message_unique")
    g.conn.execute("DROP INDEX IF EXISTS idx_derivations_source_message_unique")
    g.conn.commit()
    yield g
    g.close()


def _make_source(db: GraphDB, *, platform: str = "codex-cli", title: str = "test") -> str:
    source = Source(type="session", platform=platform, title=title,
                     file_path=f"/tmp/{title}.jsonl")
    db.insert_source(source)
    return source.id


def _thought(db: GraphDB, source_id: str, *, turn: int, message_id: str, content: str = "x"):
    t = Thought(source_id=source_id, content=content, turn_number=turn, message_id=message_id)
    db.insert_thought(t)
    return t


def _derivation(db: GraphDB, source_id: str, *, turn: int, message_id: str, content: str = "y"):
    d = Derivation(source_id=source_id, content=content, turn_number=turn, message_id=message_id)
    db.insert_derivation(d)
    return d


class TestFindDuplicateGroups:
    def test_no_duplicates_returns_empty(self, db):
        source_id = _make_source(db)
        _thought(db, source_id, turn=1, message_id="m1")
        _thought(db, source_id, turn=2, message_id="m2")
        assert find_duplicate_groups(db, source_id) == []

    def test_finds_thought_duplicate(self, db):
        source_id = _make_source(db)
        _thought(db, source_id, turn=1, message_id="dup")
        _thought(db, source_id, turn=4, message_id="dup")
        groups = find_duplicate_groups(db, source_id)
        assert len(groups) == 1
        assert len(groups[0]) == 2
        assert {r["turn_number"] for r in groups[0]} == {1, 4}

    def test_finds_derivation_duplicate(self, db):
        source_id = _make_source(db)
        _derivation(db, source_id, turn=2, message_id="dup")
        _derivation(db, source_id, turn=5, message_id="dup")
        groups = find_duplicate_groups(db, source_id)
        assert len(groups) == 1

    def test_ignores_null_message_id(self, db):
        source_id = _make_source(db)
        _thought(db, source_id, turn=1, message_id=None)
        _thought(db, source_id, turn=2, message_id=None)
        assert find_duplicate_groups(db, source_id) == []

    def test_does_not_cross_sources(self, db):
        source_a = _make_source(db, title="a")
        source_b = _make_source(db, title="b")
        _thought(db, source_a, turn=1, message_id="shared")
        _thought(db, source_b, turn=1, message_id="shared")
        assert find_duplicate_groups(db, source_a) == []
        assert find_duplicate_groups(db, source_b) == []


class TestAuditDb:
    def test_reports_affected_sources_only(self, db, tmp_path):
        clean_id = _make_source(db, title="clean")
        _thought(db, clean_id, turn=1, message_id="m1")

        dirty_id = _make_source(db, title="dirty")
        _thought(db, dirty_id, turn=1, message_id="dup")
        _thought(db, dirty_id, turn=3, message_id="dup")
        db.commit()
        db.close()

        report = audit_db(tmp_path / "org.db")
        assert len(report) == 1
        assert report[0]["source_id"] == dirty_id
        assert report[0]["duplicate_message_ids"] == 1
        assert report[0]["rows_to_delete"] == 1

    def test_skips_non_codex_platforms(self, db, tmp_path):
        claude_id = _make_source(db, platform="claude-code", title="claude sess")
        _thought(db, claude_id, turn=1, message_id="dup")
        _thought(db, claude_id, turn=2, message_id="dup")
        db.commit()
        db.close()

        report = audit_db(tmp_path / "org.db")
        assert report == [], "claude sessions were never affected by the renumbering bug"

    def test_does_not_delete_anything(self, db, tmp_path):
        source_id = _make_source(db)
        _thought(db, source_id, turn=1, message_id="dup")
        _thought(db, source_id, turn=3, message_id="dup")
        db.commit()
        db.close()

        audit_db(tmp_path / "org.db")

        # Read-only re-check too — a RW open here would itself trigger the
        # auto-repair migration and defeat the point of this assertion.
        g = GraphDB(tmp_path / "org.db", mode="ro")
        count = g.conn.execute(
            "SELECT COUNT(*) c FROM thoughts WHERE source_id = ?", (source_id,)
        ).fetchone()["c"]
        g.close()
        assert count == 2, "audit must be strictly read-only"


class TestRepairDb:
    """repair_db opens read-write, and GraphDB's own
    _migrate_message_id_unique migration (auto-4y579) now runs the exact
    same dedupe-then-index logic automatically on every RW connection
    open — including the one repair_db makes internally. In practice the
    migration gets there first, so these tests check the FINAL on-disk
    state after calling repair_db rather than asserting on repair_db's
    own returned ``deleted`` list, which will usually be empty (nothing
    left for its explicit loop to find)."""

    def test_keeps_lowest_turn_number_deletes_rest(self, db, tmp_path):
        source_id = _make_source(db)
        _thought(db, source_id, turn=5, message_id="dup", content="renumbered copy")
        _thought(db, source_id, turn=1, message_id="dup", content="original")
        db.commit()
        db.close()

        repair_db(tmp_path / "org.db")

        g = GraphDB(tmp_path / "org.db", mode="ro")
        rows = g.conn.execute(
            "SELECT turn_number, content FROM thoughts WHERE source_id = ?", (source_id,)
        ).fetchall()
        g.close()
        assert len(rows) == 1
        assert rows[0]["turn_number"] == 1
        assert rows[0]["content"] == "original"

    def test_repair_is_idempotent(self, db, tmp_path):
        source_id = _make_source(db)
        _thought(db, source_id, turn=1, message_id="dup")
        _thought(db, source_id, turn=3, message_id="dup")
        db.commit()
        db.close()

        repair_db(tmp_path / "org.db")
        second = repair_db(tmp_path / "org.db")
        assert second == [], "re-running repair against an already-clean DB deletes nothing"

        g = GraphDB(tmp_path / "org.db", mode="ro")
        count = g.conn.execute(
            "SELECT COUNT(*) c FROM thoughts WHERE source_id = ?", (source_id,)
        ).fetchone()["c"]
        g.close()
        assert count == 1

    def test_cleans_fts_index_on_delete(self, db, tmp_path):
        """FTS triggers (thoughts_ad) must fire on whichever path performs
        the delete (the migration, in practice) — stale FTS entries for a
        deleted row would corrupt search."""
        source_id = _make_source(db)
        _thought(db, source_id, turn=1, message_id="dup", content="findme original")
        _thought(db, source_id, turn=3, message_id="dup", content="findme original")
        db.commit()
        db.close()

        repair_db(tmp_path / "org.db")

        g = GraphDB(tmp_path / "org.db", mode="ro")
        fts_rows = g.conn.execute(
            "SELECT COUNT(*) c FROM thoughts_fts WHERE thoughts_fts MATCH 'findme'"
        ).fetchone()["c"]
        g.close()
        assert fts_rows == 1, "deleted row must not linger in the FTS index"

    def test_migration_dedupes_across_all_platforms_not_just_codex(self, db, tmp_path):
        """repair_db's own loop deliberately scopes to platform='codex-cli'
        sources (the bug this tool was built for), but the UNIQUE index +
        migration it now rides on top of is universal — ANY duplicate
        message_id in ANY source gets cleaned up the moment a RW
        connection opens, codex or not. That's intentional: a broader,
        simpler invariant (no duplicate message_id per source, ever) is
        safer than one narrowly scoped to a single platform's known bug."""
        claude_id = _make_source(db, platform="claude-code", title="claude sess")
        _thought(db, claude_id, turn=1, message_id="dup")
        _thought(db, claude_id, turn=2, message_id="dup")
        db.commit()
        db.close()

        repair_db(tmp_path / "org.db")

        g = GraphDB(tmp_path / "org.db", mode="ro")
        count = g.conn.execute(
            "SELECT COUNT(*) c FROM thoughts WHERE source_id = ?", (claude_id,)
        ).fetchone()["c"]
        g.close()
        assert count == 1, "the migration's UNIQUE constraint applies to every platform"

    def test_multiple_duplicate_groups_in_one_source(self, db, tmp_path):
        source_id = _make_source(db)
        _thought(db, source_id, turn=1, message_id="dup-a")
        _thought(db, source_id, turn=4, message_id="dup-a")
        _derivation(db, source_id, turn=2, message_id="dup-b")
        _derivation(db, source_id, turn=5, message_id="dup-b")
        db.commit()
        db.close()

        repair_db(tmp_path / "org.db")

        g = GraphDB(tmp_path / "org.db", mode="ro")
        thought_turns = {r["turn_number"] for r in g.conn.execute(
            "SELECT turn_number FROM thoughts WHERE source_id = ?", (source_id,)
        ).fetchall()}
        deriv_turns = {r["turn_number"] for r in g.conn.execute(
            "SELECT turn_number FROM derivations WHERE source_id = ?", (source_id,)
        ).fetchall()}
        g.close()
        assert thought_turns == {1}
        assert deriv_turns == {2}
