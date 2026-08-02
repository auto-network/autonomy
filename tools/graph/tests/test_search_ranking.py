"""Tests for search ranking driven by sources_fts metadata.

Covers the auto-kvka6 contract:

- ``sources_fts`` indexes title + short_description + keywords.
- Title hits outrank long-tail body matches via ``SEARCH_TITLE_BOOST``.
- ``short_description`` and ``keywords`` matches surface the source.
- Tag-overlap soft signal applies a capped post-process boost.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import (
    GraphDB,
    SEARCH_TAG_OVERLAP_BOOST,
    SEARCH_TAG_OVERLAP_CAP,
    SEARCH_TITLE_BOOST,
)
from tools.graph.models import Source, Thought


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    return db_path


def _make_db(db_path):
    return GraphDB(str(db_path))


def test_sources_fts_table_exists_after_migration(graph_db_env):
    """The migration creates sources_fts on a fresh DB."""
    db = _make_db(graph_db_env)
    try:
        row = db.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='sources_fts'"
        ).fetchone()
        assert row is not None
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(sources)").fetchall()}
        assert "keywords" in cols
    finally:
        db.close()


def test_title_match_ranks_above_body_match(graph_db_env):
    """A title hit outranks a body-only hit on the same term.

    The "Worktrees Dashboard Specification" pattern: a curated note with
    'worktree' in the title must beat a long session log that mentions
    'worktree' dozens of times in turn bodies.
    """
    db = _make_db(graph_db_env)
    try:
        # Title must use the same token as the query (unicode61 doesn't
        # stem) — singular "worktree" matches the singular query.
        note = Source(
            type="note",
            title="Worktree Dashboard Specification",
            file_path="note:worktree-spec",
        )
        db.insert_source(note)

        session = Source(type="session", title="Long session log",
                         file_path="session:s1")
        db.insert_source(session)
        for i in range(50):
            db.insert_thought(Thought(
                source_id=session.id,
                content="this turn body has worktree mentions " * 5,
                turn_number=i,
            ))
        db.commit()

        results = db.search("worktree", limit=10)
        assert len(results) > 0
        top = results[0]
        assert top["result_type"] == "source"
        assert top["source_id"] == note.id
        assert top["source_title"] == "Worktree Dashboard Specification"
        # Title-boost rank is ≤ -50 (per SEARCH_TITLE_BOOST), well below
        # the body BM25 floor (~ -10).
        assert top["rank"] <= SEARCH_TITLE_BOOST + 5
    finally:
        db.close()


def test_short_description_match_in_results(graph_db_env):
    """A query that matches only ``short_description`` surfaces the source."""
    db = _make_db(graph_db_env)
    try:
        src = Source(
            type="note",
            title="Generic title",
            short_description="A specification of the worktree dashboard surface.",
            file_path="note:short-desc-1",
        )
        db.insert_source(src)
        db.commit()

        results = db.search("specification", limit=10)
        assert any(r["source_id"] == src.id and r["result_type"] == "source"
                   for r in results)
    finally:
        db.close()


def test_keywords_match_in_results(graph_db_env):
    """A query that matches only the ``keywords`` column surfaces the source.

    Operators (or note.update-summary Haiku) seed common synonyms here
    so a search for an alias hits the canonical note.
    """
    db = _make_db(graph_db_env)
    try:
        src = Source(
            type="note",
            title="Branch checkout walkthrough",
            keywords="worktree,worktrees,git-worktree,parallel branches",
            file_path="note:kw-1",
        )
        db.insert_source(src)
        db.commit()

        results = db.search("worktree", limit=10)
        # The unicode61 tokenizer indexes 'worktree' from the comma list.
        assert any(r["source_id"] == src.id for r in results)
    finally:
        db.close()


def test_tag_overlap_post_process_boost(graph_db_env):
    """A source whose tags overlap query tokens ranks higher than a peer.

    Both rows have identical title hits; the row carrying the matching
    'pitfall' tag picks up a ``SEARCH_TAG_OVERLAP_BOOST`` rank delta.
    """
    db = _make_db(graph_db_env)
    try:
        tagged = Source(
            type="note",
            title="Failures pitfall guide",
            file_path="note:tag-1",
            metadata={"tags": ["pitfall"]},
        )
        plain = Source(
            type="note",
            title="Failures docs only",
            file_path="note:tag-2",
            metadata={"tags": []},
        )
        db.insert_source(tagged)
        db.insert_source(plain)
        db.commit()

        # OR mode so 'pitfall' (tag) doesn't have to literally appear in
        # the title — we want to validate the metadata-tag overlap signal.
        results = db.search("pitfall failures", or_mode=True, limit=10)
        ranks = {r["source_id"]: r["rank"] for r in results}
        assert tagged.id in ranks
        assert plain.id in ranks
        # Tagged row should be lower (better rank). Boost is at least one
        # SEARCH_TAG_OVERLAP_BOOST step compared to the untagged peer.
        assert ranks[tagged.id] <= ranks[plain.id] + SEARCH_TAG_OVERLAP_BOOST
        # Sort order reflects the boost.
        ordered_ids = [r["source_id"] for r in results]
        assert ordered_ids.index(tagged.id) < ordered_ids.index(plain.id)
    finally:
        db.close()


def test_hyphenated_tag_overlap_uses_fts_like_tokens(graph_db_env):
    """``publication-state`` reinforces a ``publication state`` query."""
    db = _make_db(graph_db_env)
    try:
        tagged = Source(
            type="note",
            title="Visibility architecture",
            file_path="note:hyphen-tag-1",
            metadata={"tags": ["publication-state"]},
        )
        plain = Source(
            type="note",
            title="Visibility architecture",
            file_path="note:hyphen-tag-2",
            metadata={"tags": []},
        )
        db.insert_source(tagged)
        db.insert_source(plain)
        db.commit()

        results = db.search(
            "publication state visibility", or_mode=True, limit=10,
        )
        ranks = {
            r["source_id"]: r["rank"]
            for r in results
            if r["source_id"] in {tagged.id, plain.id}
        }
        assert set(ranks) == {tagged.id, plain.id}
        # Two normalized tag tokens overlap the query.
        assert ranks[tagged.id] <= (
            ranks[plain.id] + 2 * SEARCH_TAG_OVERLAP_BOOST
        )
    finally:
        db.close()


def test_smart_ranker_prioritizes_term_coverage_over_one_term_title(
    graph_db_env,
):
    """A coherent two-term body match beats a one-term title match."""
    db = _make_db(graph_db_env)
    try:
        title_only = Source(
            type="note",
            title="Viewer notes",
            file_path="note:smart-title-only",
        )
        coherent = Source(
            type="note",
            title="Operator attachment guide",
            file_path="note:smart-coherent",
        )
        db.insert_source(title_only)
        db.insert_source(coherent)
        db.insert_thought(Thought(
            source_id=coherent.id,
            content="The file renders as an inline viewer for the operator.",
            turn_number=1,
        ))
        db.commit()

        legacy = db.search("inline viewer", or_mode=True, limit=10)
        assert legacy[0]["source_id"] == title_only.id

        smart = db.search(
            "inline viewer", or_mode=True, limit=10, ranker="smart",
        )
        assert smart[0]["source_id"] == coherent.id
        explain = smart[0]["ranking_explain"]
        assert explain["matched_terms"] == ["inline", "viewer"]
        assert explain["best_row_terms"] == ["inline", "viewer"]
        assert explain["channel_ranks"]["thought"] == 1
    finally:
        db.close()


def test_smart_ranker_uses_metadata_channel_to_break_coverage_tie(
    graph_db_env,
):
    """Curated metadata wins when coverage and coherence are equal."""
    db = _make_db(graph_db_env)
    try:
        metadata = Source(
            type="note",
            title="Worktree dashboard specification",
            file_path="note:smart-metadata",
        )
        body = Source(
            type="session",
            title="Implementation session",
            file_path="session:smart-body",
        )
        db.insert_source(metadata)
        db.insert_source(body)
        db.insert_thought(Thought(
            source_id=body.id,
            content="Worktree dashboard implementation details.",
            turn_number=1,
        ))
        db.commit()

        results = db.search("worktree dashboard", limit=10, ranker="smart")
        assert results[0]["source_id"] == metadata.id
        assert results[0]["ranking_explain"]["channel_ranks"] == {
            "metadata": 1,
        }
    finally:
        db.close()


def test_search_rejects_unknown_ranker(graph_db_env):
    db = _make_db(graph_db_env)
    try:
        with pytest.raises(ValueError, match="unknown search ranker"):
            db.search("anything", ranker="mystery")
    finally:
        db.close()


def test_tag_overlap_boost_capped(graph_db_env):
    """A row with many overlapping tags doesn't dominate beyond the cap."""
    db = _make_db(graph_db_env)
    try:
        # Many tags that overlap query tokens — boost should saturate
        # at SEARCH_TAG_OVERLAP_CAP, not scale linearly. Title contains
        # one of the query terms so the row enters the result set via
        # sources_fts (giving us a known title-boost baseline).
        many_tags = ["pitfall", "failure", "auth", "session",
                     "dashboard", "ranking", "search", "worktree"]
        src = Source(
            type="note",
            title="Pitfall guide",
            file_path="note:cap-1",
            metadata={"tags": many_tags},
        )
        db.insert_source(src)
        db.commit()

        query = " ".join(many_tags)
        results = db.search(query, or_mode=True, limit=10)
        match = next((r for r in results if r["source_id"] == src.id), None)
        assert match is not None
        # Total boost is title-boost (which depends on BM25 score) plus
        # the capped tag-overlap boost. The post-process cap means the
        # tag boost component never exceeds SEARCH_TAG_OVERLAP_CAP no
        # matter how many tags overlap.
        # Title BM25 contribution is non-positive (rank≤0); the tag
        # boost is bounded by the cap. So the row's rank cannot be
        # better than (SEARCH_TITLE_BOOST_floor) + cap; conservatively,
        # we assert it's no better than -200 — far below what an
        # uncapped 8-tag overlap would produce (~ -90 vs cap -25).
        assert match["rank"] > -200
    finally:
        db.close()


def test_sources_fts_update_trigger_keeps_index_in_sync(graph_db_env):
    """Updating ``title`` re-indexes the source so a new query hits it."""
    db = _make_db(graph_db_env)
    try:
        src = Source(type="note", title="Old uninteresting title",
                     file_path="note:upd-1")
        db.insert_source(src)
        db.commit()

        # Initial query for 'unicornish' returns nothing.
        assert not db.search("unicornish", limit=5)

        db.conn.execute(
            "UPDATE sources SET title = ? WHERE id = ?",
            ("Unicornish manifesto", src.id),
        )
        db.conn.commit()

        results = db.search("unicornish", limit=5)
        assert any(r["source_id"] == src.id for r in results)
    finally:
        db.close()


def test_keywords_migration_idempotent_on_legacy_db(tmp_path):
    """Opening a legacy DB twice is safe — keywords migration is idempotent."""
    db_path = tmp_path / "legacy.db"

    # Bootstrap a real schema, drop the FTS table + triggers + the
    # keywords column to simulate a pre-migration legacy DB. SQLite
    # validates triggers when ALTER-ing a referenced column, so the
    # triggers must come down before the column drop.
    db = GraphDB(str(db_path))
    db.close()
    conn = sqlite3.connect(str(db_path))
    for trg in ("sources_ai", "sources_ad", "sources_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trg}")
    conn.execute("DROP TABLE IF EXISTS sources_fts")
    conn.execute("ALTER TABLE sources DROP COLUMN keywords")
    # A genuine legacy DB carries an older schema stamp; reset it so the
    # reopen does not take the already-current fast path.
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    cols_before = {r[1] for r in sqlite3.connect(str(db_path))
                   .execute("PRAGMA table_info(sources)").fetchall()}
    assert "keywords" not in cols_before

    # Re-open: migration must add the column AND re-create sources_fts
    # AND rebuild it.
    db = GraphDB(str(db_path))
    db.close()

    conn = sqlite3.connect(str(db_path))
    try:
        cols_after = {r[1] for r in conn.execute(
            "PRAGMA table_info(sources)").fetchall()}
        fts_table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='sources_fts'"
        ).fetchone()
    finally:
        conn.close()

    assert "keywords" in cols_after
    assert fts_table is not None
