"""Tests for ``db.search(source_type=…)`` — the new ``--type`` filter on
``graph search``.

The operator hit this orienting around a live session: a high-signal
single term surfaced the right session at hit #1, but widening with
``--or`` or extra terms scored it down. ``--type session`` scopes the
FTS hit list to the source kind the operator already has in mind, so
the right row stays at the top regardless of widening.

Pinned contracts:

1. ``source_type=['session']`` returns only ``s.type='session'`` rows;
   note / bead matches drop out.
2. ``source_type=['note', 'bead']`` is the union — a notion-style
   multi-kind filter.
3. ``source_type=None`` (default) is unchanged behaviour — every kind
   competes.
4. ``source_type=[]`` returns zero rows (mirrors ``session_type``
   semantics; explicit empty list = "match nothing", not "match all").
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


def _seed(db: GraphDB, *, title: str, kind: str, term: str) -> Source:
    src = Source(
        type=kind,
        platform="local",
        project="autonomy",
        title=title,
        file_path=f"{kind}:{title.replace(' ', '_').lower()}",
        metadata={},
    )
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


def test_source_type_filter_scopes_to_kind(graph_db_env):
    db = GraphDB(str(graph_db_env))
    try:
        sess = _seed(db, title="terminate drawer button",
                    kind="session", term="terminate")
        note = _seed(db, title="terminate semantics note",
                    kind="note", term="terminate")
        bead = _seed(db, title="terminate fix bead",
                    kind="bead", term="terminate")

        only_session = db.search("terminate", limit=20,
                                 source_type=["session"])
        ids = {r["source_id"] for r in only_session}
        assert sess.id in ids
        assert note.id not in ids
        assert bead.id not in ids
    finally:
        db.close()


def test_source_type_filter_accepts_union(graph_db_env):
    """``source_type=['note', 'bead']`` keeps notes AND beads."""
    db = GraphDB(str(graph_db_env))
    try:
        sess = _seed(db, title="terminate drawer", kind="session", term="terminate")
        note = _seed(db, title="terminate doc", kind="note", term="terminate")
        bead = _seed(db, title="terminate fix", kind="bead", term="terminate")

        rows = db.search("terminate", limit=20, source_type=["note", "bead"])
        ids = {r["source_id"] for r in rows}
        assert note.id in ids
        assert bead.id in ids
        assert sess.id not in ids
    finally:
        db.close()


def test_source_type_none_disables_filter(graph_db_env):
    """Default behaviour preserved — ``None`` means "every kind"."""
    db = GraphDB(str(graph_db_env))
    try:
        sess = _seed(db, title="terminate drawer", kind="session", term="terminate")
        note = _seed(db, title="terminate doc", kind="note", term="terminate")

        rows = db.search("terminate", limit=20, source_type=None)
        ids = {r["source_id"] for r in rows}
        assert sess.id in ids
        assert note.id in ids
    finally:
        db.close()


def test_source_type_empty_list_returns_nothing(graph_db_env):
    """Empty list = match nothing. Mirrors session_type semantics so a
    caller that ends up with ``[].split(",")`` doesn't quietly widen
    the search to every kind."""
    db = GraphDB(str(graph_db_env))
    try:
        _seed(db, title="terminate drawer", kind="session", term="terminate")
        rows = db.search("terminate", limit=20, source_type=[])
        assert rows == []
    finally:
        db.close()
