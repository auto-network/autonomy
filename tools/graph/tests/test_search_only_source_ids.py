"""``db.search(only_source_ids=...)`` — restrict FTS to named sources.

The sessions page's on-screen transcript search sends the graph source ids
of every card it is showing. Contracts pinned here:

1. ``None`` leaves the candidate set alone; ``[]`` returns zero rows; a
   list keeps only those sources — across the legacy and smart rankers.
2. It composes with the publication-state filter rather than replacing
   it: a raw source is only searchable when the caller also names it in
   ``session_source_ids`` (own-surface), which is what ``ops.search`` does
   for own-org DBs — and does NOT do for peers.
3. ``ops.search`` threads the restriction through to the DB.
"""

from __future__ import annotations

import pytest

from tools.graph import ops
from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    return db_path


def _seed(db: GraphDB, *, title: str, state: str = "published") -> Source:
    src = Source(
        type="session", platform="local", title=title,
        file_path=f"session:{title.replace(' ', '_')}",
        metadata={"session_type": "interactive"},
        publication_state=state,
    )
    db.insert_source(src)
    db.insert_thought(Thought(
        source_id=src.id, content="we discussed the rollback plan at length",
        role="user", turn_number=1, tags=[],
    ))
    db.commit()
    return src


def _source_ids(rows: list[dict]) -> set[str]:
    return {r["source_id"] for r in rows if r.get("source_id")}


@pytest.mark.parametrize("ranker", ["legacy", "smart"])
def test_only_source_ids_restricts_and_empty_list_matches_nothing(graph_db_env, ranker):
    db = GraphDB(str(graph_db_env))
    a = _seed(db, title="session alpha")
    b = _seed(db, title="session beta")
    c = _seed(db, title="session gamma")

    everything = db.search("rollback", ranker=ranker)
    assert _source_ids(everything) == {a.id, b.id, c.id}

    restricted = db.search("rollback", ranker=ranker, only_source_ids=[a.id, c.id])
    assert _source_ids(restricted) == {a.id, c.id}

    assert db.search("rollback", ranker=ranker, only_source_ids=[]) == []
    db.close()


def test_only_source_ids_composes_with_state_filter(graph_db_env):
    db = GraphDB(str(graph_db_env))
    live = _seed(db, title="live raw session", state="raw")
    done = _seed(db, title="published session")

    # Restriction alone never widens: the raw row stays hidden.
    rows = db.search("rollback", only_source_ids=[live.id, done.id])
    assert _source_ids(rows) == {done.id}

    # Own-surface callers name the same ids in session_source_ids.
    rows = db.search(
        "rollback", only_source_ids=[live.id, done.id],
        session_source_ids=[live.id, done.id],
    )
    assert _source_ids(rows) == {live.id, done.id}

    # And an explicit ``states`` override still composes with it.
    rows = db.search("rollback", states=["raw"], only_source_ids=[done.id])
    assert rows == []
    rows = db.search("rollback", states=["raw"], only_source_ids=[live.id])
    assert _source_ids(rows) == {live.id}
    db.close()


def test_ops_search_threads_only_source_ids_for_own_surface(graph_db_env):
    db = GraphDB(str(graph_db_env))
    live = _seed(db, title="live raw session", state="raw")
    other = _seed(db, title="another live session", state="raw")
    _seed(db, title="published session")
    db.close()

    rows = ops.search("rollback", only_source_ids=[live.id])
    assert _source_ids(rows) == {live.id}

    rows = ops.search("rollback", only_source_ids=[live.id, other.id])
    assert _source_ids(rows) == {live.id, other.id}

    assert ops.search("rollback", only_source_ids=[]) == []
