"""Tests for the publication_state scope+facet primitive (auto-xdoo).

Covers schema migration, CHECK constraints, and the default search-surface
filter that hides raw content from cross-session queries. Decision note:
graph://8cf067e3-ca3.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


@pytest.fixture
def fresh_db(tmp_path):
    db = GraphDB(tmp_path / "graph.db")
    yield db
    db.close()


@pytest.fixture
def legacy_db_path(tmp_path):
    """Create a DB with the schema as it existed before publication_state landed."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE sources (
            id               TEXT PRIMARY KEY,
            type             TEXT NOT NULL,
            platform         TEXT,
            project          TEXT,
            title            TEXT,
            url              TEXT,
            file_path        TEXT UNIQUE,
            metadata         TEXT DEFAULT '{}',
            created_at       TEXT NOT NULL,
            ingested_at      TEXT NOT NULL,
            last_activity_at TEXT
        );

        CREATE TABLE thoughts (
            id          TEXT PRIMARY KEY,
            source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            content     TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'user',
            turn_number INTEGER,
            message_id  TEXT,
            tags        TEXT DEFAULT '[]',
            metadata    TEXT DEFAULT '{}',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE note_comments (
            id         TEXT PRIMARY KEY,
            source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            content    TEXT NOT NULL,
            actor      TEXT DEFAULT 'user',
            integrated INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE captures (
            id         TEXT PRIMARY KEY,
            content    TEXT NOT NULL,
            thread_id  TEXT,
            source_id  TEXT,
            turn_number INTEGER,
            status     TEXT NOT NULL DEFAULT 'captured',
            actor      TEXT DEFAULT 'user',
            metadata   TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO sources(id, type, platform, metadata, created_at, ingested_at)"
        " VALUES('legacy_note', 'note', 'local', '{\"tags\": [\"pitfall\"]}', '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO thoughts(id, source_id, content, role, created_at)"
        " VALUES('t1', 'legacy_note', 'legacy body', 'user', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO note_comments(id, source_id, content, actor, created_at)"
        " VALUES('c1', 'legacy_note', 'a comment', 'user', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO captures(id, content, created_at) VALUES('cap1', 'caught', '2026-01-01')"
    )
    conn.commit()
    conn.close()
    return path


# ── Schema ──────────────────────────────────────────────────────────────


def test_fresh_db_has_publication_state_columns(fresh_db):
    cols = {r[1] for r in fresh_db.conn.execute("PRAGMA table_info(sources)").fetchall()}
    assert {"publication_state", "deprecated", "successor_id", "moved_to_org"} <= cols

    for table in ("thoughts", "note_comments", "captures"):
        tcols = {r[1] for r in fresh_db.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert "publication_state" in tcols, f"{table} missing publication_state"


def test_fresh_db_has_state_index(fresh_db):
    idx = fresh_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sources_publication_state'"
    ).fetchone()
    assert idx is not None


def test_legacy_db_migrates_on_open(legacy_db_path):
    db = GraphDB(legacy_db_path)
    try:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(sources)").fetchall()}
        assert {"publication_state", "deprecated", "successor_id", "moved_to_org"} <= cols
        row = db.conn.execute(
            "SELECT publication_state, deprecated, successor_id, moved_to_org FROM sources WHERE id='legacy_note'"
        ).fetchone()
        # Default flipped to 'curated' (graph://8cf067e3-ca3 follow-up): every
        # new/migrated source row lands cross-session-visible unless explicitly
        # held back in 'raw'. Substrate tables (thoughts/comments/captures)
        # remain CHECK-pinned to 'raw' below.
        assert row["publication_state"] == "curated"
        assert row["deprecated"] == 0
        assert row["successor_id"] is None
        assert row["moved_to_org"] is None

        # thoughts / comments / captures all get 'raw' default
        for table in ("thoughts", "note_comments", "captures"):
            r = db.conn.execute(f"SELECT publication_state FROM {table}").fetchone()
            assert r["publication_state"] == "raw"
    finally:
        db.close()


def test_migration_is_idempotent(legacy_db_path):
    db = GraphDB(legacy_db_path)
    cols_after_first = {r[1] for r in db.conn.execute("PRAGMA table_info(sources)").fetchall()}
    db.close()

    db2 = GraphDB(legacy_db_path)
    try:
        cols_after_second = {r[1] for r in db2.conn.execute("PRAGMA table_info(sources)").fetchall()}
        assert cols_after_first == cols_after_second
    finally:
        db2.close()


# ── CHECK constraints ───────────────────────────────────────────────────


def test_sources_rejects_invalid_state(fresh_db):
    src = Source(type="note", title="x", file_path="note:x")
    fresh_db.insert_source(src)
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.conn.execute(
            "UPDATE sources SET publication_state='invalid' WHERE id=?", (src.id,)
        )


def test_sources_allows_all_four_states(fresh_db):
    src = Source(type="note", title="x", file_path="note:x")
    fresh_db.insert_source(src)
    for state in ("raw", "curated", "published", "canonical"):
        fresh_db.conn.execute(
            "UPDATE sources SET publication_state=? WHERE id=?", (state, src.id)
        )


@pytest.mark.parametrize("table", ["thoughts", "note_comments", "captures"])
def test_fixed_state_tables_reject_non_raw(fresh_db, table):
    src = Source(type="note", title="fixed", file_path="note:fixed")
    fresh_db.insert_source(src)
    if table == "thoughts":
        # Insert then try to mutate
        t = Thought(source_id=src.id, content="x", role="user", turn_number=1)
        fresh_db.insert_thought(t)
        with pytest.raises(sqlite3.IntegrityError):
            fresh_db.conn.execute(
                "UPDATE thoughts SET publication_state='curated' WHERE id=?", (t.id,)
            )
    elif table == "note_comments":
        fresh_db.insert_comment(src.id, "comment body")
        with pytest.raises(sqlite3.IntegrityError):
            fresh_db.conn.execute(
                "UPDATE note_comments SET publication_state='canonical' WHERE source_id=?", (src.id,)
            )
    else:  # captures
        fresh_db.insert_capture("cap_x", "caught")
        with pytest.raises(sqlite3.IntegrityError):
            fresh_db.conn.execute(
                "UPDATE captures SET publication_state='published' WHERE id=?", ("cap_x",)
            )


def test_new_sources_default_to_curated(fresh_db):
    """New sources default to ``curated`` so ``graph note`` output is
    visible in cross-session reads by default. See follow-up to
    graph://8cf067e3-ca3 — ``raw`` is reserved for genuinely incomplete
    drafts; operators explicitly mark those with ``publication_state="raw"``.
    """
    src = Source(type="note", title="new", file_path="note:new")
    fresh_db.insert_source(src)
    row = fresh_db.conn.execute(
        "SELECT publication_state FROM sources WHERE id=?", (src.id,)
    ).fetchone()
    assert row["publication_state"] == "curated"


# ── Search / list filter behavior ───────────────────────────────────────


def _seed_fts_source(db, *, source_id, title, content, author, state="raw"):
    """Create a note-type source with an FTS-indexed thought under it."""
    src = Source(
        id=source_id,
        type="note",
        platform="local",
        project=None,
        title=title,
        file_path=f"note:{source_id}",
        metadata={"author": author, "tags": []},
        publication_state=state,
    )
    db.insert_source(src)
    t = Thought(source_id=src.id, content=content, role="user", turn_number=1)
    db.insert_thought(t)
    db.commit()


def test_search_default_excludes_raw_from_other_sessions(fresh_db):
    _seed_fts_source(fresh_db, source_id="own_raw", title="Own raw",
                     content="widget alpha", author="terminal:session-me")
    _seed_fts_source(fresh_db, source_id="other_raw", title="Other raw",
                     content="widget beta", author="terminal:session-you")
    _seed_fts_source(fresh_db, source_id="other_canonical", title="Other canonical",
                     content="widget gamma", author="terminal:session-you", state="canonical")

    # Simulate being inside session-me
    results = fresh_db.search(
        "widget", limit=50,
        session_author_pattern="%session-me%",
    )
    ids = {r["source_id"] for r in results}
    assert "own_raw" in ids            # own raw is visible
    assert "other_raw" not in ids      # other raw is hidden
    assert "other_canonical" in ids    # curated/canonical from others is visible


def test_search_include_raw_shows_everything(fresh_db):
    _seed_fts_source(fresh_db, source_id="a", title="A", content="zeta one",
                     author="terminal:me")
    _seed_fts_source(fresh_db, source_id="b", title="B", content="zeta two",
                     author="terminal:other")
    results = fresh_db.search("zeta", limit=50, include_raw=True)
    ids = {r["source_id"] for r in results}
    assert ids >= {"a", "b"}


def test_search_state_filter_only_returns_requested(fresh_db):
    _seed_fts_source(fresh_db, source_id="r1", title="r1", content="eta",
                     author="user", state="raw")
    _seed_fts_source(fresh_db, source_id="c1", title="c1", content="eta",
                     author="user", state="canonical")
    _seed_fts_source(fresh_db, source_id="p1", title="p1", content="eta",
                     author="user", state="published")

    only_canonical = fresh_db.search("eta", limit=50, states=["canonical"])
    assert {r["source_id"] for r in only_canonical} == {"c1"}

    pub_or_canon = fresh_db.search("eta", limit=50, states=["published", "canonical"])
    assert {r["source_id"] for r in pub_or_canon} == {"c1", "p1"}


def test_list_sources_hides_raw_from_other_sessions(fresh_db):
    _seed_fts_source(fresh_db, source_id="mine", title="mine", content="x",
                     author="terminal:me")
    _seed_fts_source(fresh_db, source_id="theirs_raw", title="theirs_raw",
                     content="x", author="terminal:them")
    _seed_fts_source(fresh_db, source_id="theirs_canon", title="theirs_canon",
                     content="x", author="terminal:them", state="canonical")

    rows = fresh_db.list_sources(source_type="note",
                                 session_author_pattern="%me%")
    ids = {r["id"] for r in rows}
    assert "mine" in ids
    assert "theirs_canon" in ids
    assert "theirs_raw" not in ids


def test_list_sources_state_filter(fresh_db):
    _seed_fts_source(fresh_db, source_id="r", title="r", content="x",
                     author="user", state="raw")
    _seed_fts_source(fresh_db, source_id="c", title="c", content="x",
                     author="user", state="canonical")
    rows = fresh_db.list_sources(source_type="note", states=["canonical"])
    ids = {r["id"] for r in rows}
    assert ids == {"c"}


def test_session_membership_includes_raw_by_source_id(fresh_db):
    """A raw source owned by the current session (by id) is visible even when
    the author field doesn't match."""
    _seed_fts_source(fresh_db, source_id="session_owned", title="so",
                     content="kappa", author="agent")
    results = fresh_db.search("kappa", limit=50,
                              session_source_ids=["session_owned"])
    assert {r["source_id"] for r in results} == {"session_owned"}
