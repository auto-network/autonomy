"""Tests for the search result data shape (auto-lpzcc).

Three shape bugs fixed here:

1. ``_is_source_id`` must accept canonical 5-segment UUIDs so that direct
   source-ID lookups don't fall through to FTS and return audit-log junk.
2. Every FTS hit must carry ``source_type``, ``source_created_at`` and
   ``source_metadata`` so the dashboard can colour cards by kind and show
   a creation date.
3. A canonical UUID search must resolve to a ``result_type == "source"``
   row, not a thought/derivation FTS hit.
"""

from __future__ import annotations

import pytest

from tools.graph import ops
from tools.graph.db import GraphDB, _is_source_id
from tools.graph.models import Source, Thought


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    return db_path


def _seed_source_with_thought(
    db: GraphDB, *, type_: str, title: str, term: str, project: str = "autonomy"
) -> Source:
    src = Source(
        type=type_,
        platform="local",
        title=title,
        file_path=f"{type_}:{title.replace(' ', '_')}",
        metadata={"tags": ["test"]},
    )
    db.insert_source(src)
    db.insert_thought(Thought(
        source_id=src.id,
        content=f"discussion about {term}",
        role="user",
        turn_number=1,
        tags=["test"],
    ))
    # ``insert_thought`` does not auto-commit — flush so that each call
    # is durable across the close+reopen that ops.search performs.
    db.conn.commit()
    return src


def test_source_id_regex_matches_canonical_uuid():
    """``_is_source_id`` must accept canonical 5-segment UUIDs."""
    assert _is_source_id("38c10838-0945-4cc5-aad4-61fd323ba875") is True
    assert _is_source_id("38c10838-094") is True
    # bead IDs are not source IDs
    assert _is_source_id("auto-83g69") is False
    # plain words are not source IDs
    assert _is_source_id("CVSS") is False


def test_search_returns_source_type_for_fts_hits(graph_db_env):
    """FTS hits must carry the parent source's ``type`` so cards can be
    coloured by note vs session vs agent-run, etc."""
    db = GraphDB(str(graph_db_env))
    _seed_source_with_thought(db, type_="note", title="passkey note", term="passkey")
    _seed_source_with_thought(db, type_="session", title="passkey session", term="passkey")
    db.close()

    results = ops.search("passkey", include_raw=True)

    types_seen = {r.get("source_type") for r in results}
    assert "note" in types_seen
    assert "session" in types_seen
    # No FTS hit should be missing ``source_type``.
    for r in results:
        if r.get("result_type") in ("thought", "derivation"):
            assert r.get("source_type"), f"FTS hit missing source_type: {r!r}"


def test_search_returns_source_created_at_for_fts_hits(graph_db_env):
    """FTS hits must carry a non-empty ``source_created_at``."""
    db = GraphDB(str(graph_db_env))
    _seed_source_with_thought(db, type_="note", title="dated note", term="datedterm")
    db.close()

    results = ops.search("datedterm", include_raw=True)

    fts_hits = [r for r in results if r.get("result_type") in ("thought", "derivation")]
    assert fts_hits, "expected at least one FTS hit"
    for r in fts_hits:
        assert r.get("source_created_at"), \
            f"FTS hit missing source_created_at: {r!r}"


def test_search_canonical_uuid_resolves_directly(graph_db_env):
    """Searching for a canonical 5-segment UUID must resolve via the
    direct source-ID path (``result_type == "source"``), not FTS."""
    db = GraphDB(str(graph_db_env))
    src = _seed_source_with_thought(db, type_="note", title="uuid target", term="uuidterm")
    db.close()

    # ``src.id`` from ``new_id()`` is a canonical 5-segment UUID.
    assert src.id.count("-") == 4
    results = ops.search(src.id, include_raw=True)

    assert results, "expected at least one result for canonical UUID"
    first = results[0]
    assert first.get("result_type") == "source"
    assert first.get("source_id") == src.id
