"""Unit tests for ``tools.graph.ops``.

Each test exercises one ops function against a real ephemeral GraphDB
(SQLite is fast enough that mocking adds noise without speed). The mocking
focus here is environment isolation — each test pins ``GRAPH_DB`` to its
own tmp file so concurrent runs cannot collide.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.graph import ops
from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    return db_path


def _seed_note(db: GraphDB, *, title: str, tags: list[str], project: str = "autonomy") -> Source:
    """Insert a note source + a single thought turn for FTS coverage."""
    src = Source(
        type="note",
        platform="local",
        project=project,
        title=title,
        file_path=f"note:{title.replace(' ', '_')}",
        metadata={"tags": tags, "author": "test"},
    )
    db.insert_source(src)
    db.insert_thought(Thought(
        source_id=src.id,
        content=title,
        role="user",
        turn_number=1,
        tags=tags,
    ))
    return src


def test_search_returns_results_for_known_term(graph_db_env):
    """ops.search routes through GraphDB.search and returns FTS hits."""
    db = GraphDB(str(graph_db_env))
    _seed_note(db, title="passkey authentication design", tags=["auth"])
    _seed_note(db, title="unrelated content here", tags=["misc"])
    db.close()

    # include_raw=True: seeded notes default to publication_state='raw' and
    # would be hidden from cross-session callers under the new default filter.
    results = ops.search("passkey", include_raw=True)
    assert any("passkey" in (r.get("content") or "").lower()
               or "passkey" in (r.get("source_title") or "").lower()
               for r in results)


def test_get_source_round_trips(graph_db_env):
    """ops.get_source returns the row inserted via the DAO."""
    db = GraphDB(str(graph_db_env))
    src = _seed_note(db, title="round-trip note", tags=[])
    db.close()

    got = ops.get_source(src.id)
    assert got is not None
    assert got["id"] == src.id
    assert got["title"] == "round-trip note"


def test_get_source_missing_returns_none(graph_db_env):
    """Missing IDs return None, not a raise."""
    GraphDB(str(graph_db_env)).close()
    assert ops.get_source("00000000-0000-0000-0000-000000000000") is None


def test_list_sources_filters_by_tag(graph_db_env):
    """ops.list_sources passes tag filter through to the DAO."""
    db = GraphDB(str(graph_db_env))
    _seed_note(db, title="pitfall A", tags=["pitfall"])
    _seed_note(db, title="other note", tags=["misc"])
    db.close()

    pitfalls = ops.list_sources(source_type="note", tags=["pitfall"], include_raw=True)
    titles = [s["title"] for s in pitfalls]
    assert "pitfall A" in titles
    assert "other note" not in titles


def test_add_tag_returns_true_on_first_application(graph_db_env):
    """Tag is newly added on first call, no-op on second."""
    db = GraphDB(str(graph_db_env))
    src = _seed_note(db, title="taggable", tags=[])
    db.close()

    assert ops.add_tag(src.id, "shiny") is True
    assert ops.add_tag(src.id, "shiny") is False


def test_remove_tag_round_trips(graph_db_env):
    """add_tag → remove_tag → tag absent."""
    db = GraphDB(str(graph_db_env))
    src = _seed_note(db, title="removable", tags=[])
    db.close()

    ops.add_tag(src.id, "ephemeral")
    assert ops.remove_tag(src.id, "ephemeral") is True
    assert ops.remove_tag(src.id, "ephemeral") is False


def test_add_comment_then_integrate(graph_db_env):
    """Comment lifecycle: add → integrate → integrated flag flips."""
    db = GraphDB(str(graph_db_env))
    src = _seed_note(db, title="commentable", tags=[])
    db.close()

    comment = ops.add_comment(src.id, "first thought", actor="tester")
    assert comment["source_id"] == src.id
    assert comment["integrated"] == 0

    assert ops.integrate_comment(comment["id"]) is True
    # Idempotent: second integrate returns False
    assert ops.integrate_comment(comment["id"]) is False


def test_get_attachment_missing(graph_db_env):
    """Returns None for unknown attachment id (no raise)."""
    GraphDB(str(graph_db_env)).close()
    assert ops.get_attachment("missing-id") is None


def test_streams_summary_aggregates_tags(graph_db_env):
    """streams_summary counts tag occurrences across notes."""
    db = GraphDB(str(graph_db_env))
    _seed_note(db, title="a", tags=["alpha", "beta"])
    _seed_note(db, title="b", tags=["alpha"])
    db.close()

    streams = ops.streams_summary()
    by_tag = {s["tag"]: s["count"] for s in streams}
    assert by_tag.get("alpha") == 2
    assert by_tag.get("beta") == 1


def test_caller_org_kwarg_accepted(graph_db_env):
    """caller_org and peers parameters are accepted (placeholder for per-org DB).

    Today they are ignored — verify the signatures accept them without error
    so downstream beads can pass them through call sites.
    """
    GraphDB(str(graph_db_env)).close()
    # Should not raise
    ops.search("anything", caller_org="autonomy", peers=["anchore"])
    ops.list_sources(caller_org="autonomy", peers=["anchore"], limit=1)
    ops.get_source("missing", caller_org="autonomy", peers=None)
