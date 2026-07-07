"""Tests for ``db.search`` ordering + ``session_type`` filter (auto-fsw6r).

Round 7k pinned two new contracts on the search surface:

1. ``order='relevance'`` (the default) ranks by FTS BM25 + boosts;
   ``order='recent'`` orders by ``source.created_at DESC``; an unknown
   value raises ``ValueError`` rather than silently degrading.

2. ``session_type`` (list[str] | None) is a strict filter on
   ``metadata.session_type``: ``None`` disables the filter, ``[]``
   returns zero rows, and a non-empty list NEVER matches a row whose
   ``session_type`` is NULL/missing — the SQL is NULL-safe by design
   (``json_extract`` returns NULL, and SQL ``IN`` rejects NULL). Pinning
   that contract here means the data-hygiene bead for ~615 NULL-
   session_type rows in autonomy.db can land independently without
   changing search semantics.
"""

from __future__ import annotations

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    return db_path


def _seed_session(
    db: GraphDB, *, title: str, term: str,
    session_type: str | None,
    created_at: str | None = None,
    type_: str = "session",
) -> Source:
    metadata: dict = {}
    if session_type is not None:
        metadata["session_type"] = session_type
    kwargs: dict = dict(
        type=type_,
        platform="local",
        title=title,
        file_path=f"{type_}:{title.replace(' ', '_').lower()}",
        metadata=metadata,
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    src = Source(**kwargs)
    db.insert_source(src)
    db.insert_thought(Thought(
        source_id=src.id,
        content=f"discussion about {term}",
        role="user",
        turn_number=1,
        tags=[],
    ))
    db.commit()
    return src


# ── Ordering ──────────────────────────────────────────────────────────


def test_search_default_order_is_relevance(graph_db_env):
    """No ``order`` arg → relevance (BM25). Title-boosted hit ranks first."""
    db = GraphDB(str(graph_db_env))
    try:
        # Title contains the query term — gets the SEARCH_TITLE_BOOST step.
        boosted = _seed_session(
            db, title="passkey ranking",
            term="passkey", session_type=None,
            created_at="2026-01-01T00:00:00Z",  # oldest
        )
        # Body-only match (title has no overlap) — rank floor.
        body_only = _seed_session(
            db, title="unrelated heading",
            term="passkey", session_type=None,
            created_at="2026-04-29T00:00:00Z",  # most recent
        )

        results = db.search("passkey", limit=10)
        ids = [r["source_id"] for r in results]
        assert boosted.id in ids
        assert body_only.id in ids
        # Title-boosted row outranks the body-only row even though it's
        # older — relevance is the default.
        assert ids.index(boosted.id) < ids.index(body_only.id), (
            f"default order is not relevance; got {ids!r}"
        )
    finally:
        db.close()


def test_search_recent_orders_by_created_at_desc(graph_db_env):
    """``order='recent'`` sorts strongest-rank LAST when the recent row
    has lower BM25 — recency wins over relevance under the toggle."""
    db = GraphDB(str(graph_db_env))
    try:
        old_titlematch = _seed_session(
            db, title="passkey early note",
            term="passkey", session_type=None,
            created_at="2026-01-01T00:00:00Z",
        )
        recent_bodymatch = _seed_session(
            db, title="unrelated heading",
            term="passkey", session_type=None,
            created_at="2026-04-29T00:00:00Z",
        )

        results = db.search("passkey", limit=10, order="recent")
        ids = [r["source_id"] for r in results]
        assert recent_bodymatch.id in ids
        assert old_titlematch.id in ids
        # The most-recent row comes first under recency, regardless of
        # BM25 strength.
        assert ids[0] == recent_bodymatch.id, (
            f"recent ordering did not surface newest first; got {ids!r}"
        )
        # The older title-boosted row still appears, but later.
        assert ids.index(recent_bodymatch.id) < ids.index(old_titlematch.id)
    finally:
        db.close()


def test_search_unknown_order_raises(graph_db_env):
    """``order`` outside the allowlist raises ``ValueError`` (not a silent
    fallback). Catches typos like ``order='date'`` or ``order='desc'``."""
    db = GraphDB(str(graph_db_env))
    try:
        _seed_session(
            db, title="passkey", term="passkey",
            session_type=None,
        )
        with pytest.raises(ValueError):
            db.search("passkey", order="date")
        with pytest.raises(ValueError):
            db.search("passkey", order="desc")
        with pytest.raises(ValueError):
            db.search("passkey", order="")
    finally:
        db.close()


# ── session_type filter ──────────────────────────────────────────────


def test_session_type_subset_match(graph_db_env):
    """A ``session_type=['terminal','chatwith']`` filter returns ONLY
    rows whose ``metadata.session_type`` is in that list — rows tagged
    'dispatch' / 'librarian' / 'agentic' must be excluded.
    """
    db = GraphDB(str(graph_db_env))
    try:
        terminal = _seed_session(
            db, title="terminal session bandwidth",
            term="bandwidth", session_type="terminal",
        )
        chatwith = _seed_session(
            db, title="chatwith session bandwidth",
            term="bandwidth", session_type="chatwith",
        )
        dispatch = _seed_session(
            db, title="dispatch run bandwidth",
            term="bandwidth", session_type="dispatch",
        )
        librarian = _seed_session(
            db, title="librarian session bandwidth",
            term="bandwidth", session_type="librarian",
        )

        results = db.search(
            "bandwidth", limit=20, session_type=["terminal", "chatwith"],
        )
        ids = {r["source_id"] for r in results}
        assert terminal.id in ids
        assert chatwith.id in ids
        assert dispatch.id not in ids, (
            f"dispatch row leaked into terminal/chatwith filter; got {ids!r}"
        )
        assert librarian.id not in ids
    finally:
        db.close()


def test_session_type_dispatch_subset_match(graph_db_env):
    """The Dispatch pill's session_type filter
    (['dispatch','librarian','agentic']) must match all three of those
    values and exclude interactive rows + NULL-session_type rows.
    """
    db = GraphDB(str(graph_db_env))
    try:
        dispatch = _seed_session(
            db, title="dispatch alpha",
            term="alpha", session_type="dispatch",
        )
        librarian = _seed_session(
            db, title="librarian alpha",
            term="alpha", session_type="librarian",
        )
        # ``agentic`` source-type rows are excluded by db.py default
        # (auxiliary types). Pass ``excluded_source_types=[]`` to surface
        # them so the filter under test gets a chance to match.
        agentic = _seed_session(
            db, title="agentic alpha",
            term="alpha", session_type="agentic",
            type_="agentic",
        )
        terminal = _seed_session(
            db, title="terminal alpha",
            term="alpha", session_type="terminal",
        )
        null_st = _seed_session(
            db, title="null session alpha",
            term="alpha", session_type=None,
        )

        results = db.search(
            "alpha", limit=20,
            session_type=["dispatch", "librarian", "agentic"],
            excluded_source_types=[],
        )
        ids = {r["source_id"] for r in results}
        assert dispatch.id in ids
        assert librarian.id in ids
        assert agentic.id in ids
        assert terminal.id not in ids
        assert null_st.id not in ids
    finally:
        db.close()


def test_session_type_empty_list_returns_zero(graph_db_env):
    """``session_type=[]`` is the empty-allowlist sentinel — every row
    fails the filter so the result list is empty. Distinct from ``None``
    (no filter) so callers can express "explicitly nothing"."""
    db = GraphDB(str(graph_db_env))
    try:
        _seed_session(
            db, title="alpha row",
            term="alpha", session_type="terminal",
        )
        _seed_session(
            db, title="alpha other",
            term="alpha", session_type=None,
        )

        results = db.search("alpha", limit=20, session_type=[])
        assert results == [], f"empty filter returned rows: {results!r}"
    finally:
        db.close()


def test_session_type_null_invisible_when_filter_active(graph_db_env):
    """NULL-session_type rows MUST be invisible to a non-empty
    session_type filter. Pinning this contract: the read-side fallback
    ("if NULL, treat as interactive") was intentionally NOT implemented
    in Round 7k — the data-hygiene bead for the ~615 NULL rows can land
    independently without changing search behaviour.
    """
    db = GraphDB(str(graph_db_env))
    try:
        present = _seed_session(
            db, title="present sessiontype delta",
            term="delta", session_type="terminal",
        )
        missing = _seed_session(
            db, title="missing sessiontype delta",
            term="delta", session_type=None,
        )

        # Without the filter, both rows surface.
        unfiltered = db.search("delta", limit=20)
        ids_unfiltered = {r["source_id"] for r in unfiltered}
        assert present.id in ids_unfiltered
        assert missing.id in ids_unfiltered

        # With ``session_type=['terminal']``, only the row that has
        # ``metadata.session_type='terminal'`` is returned. The
        # NULL-session_type row is invisible — even though it might
        # "look like" an interactive session by source_type.
        filtered = db.search(
            "delta", limit=20, session_type=["terminal"],
        )
        ids_filtered = {r["source_id"] for r in filtered}
        assert present.id in ids_filtered
        assert missing.id not in ids_filtered, (
            "NULL-session_type row must be invisible to the filter; "
            f"got ids={ids_filtered!r}"
        )
    finally:
        db.close()


def test_session_type_none_disables_filter(graph_db_env):
    """``session_type=None`` (the default) returns every row, including
    NULL-session_type rows — confirms the contract for "no filter"."""
    db = GraphDB(str(graph_db_env))
    try:
        present = _seed_session(
            db, title="visible epsilon",
            term="epsilon", session_type="terminal",
        )
        missing = _seed_session(
            db, title="absent epsilon",
            term="epsilon", session_type=None,
        )

        results = db.search("epsilon", limit=20, session_type=None)
        ids = {r["source_id"] for r in results}
        assert present.id in ids
        assert missing.id in ids
    finally:
        db.close()
