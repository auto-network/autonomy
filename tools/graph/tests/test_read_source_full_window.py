"""Tests for ``ops.read_source_full`` with the ``around_turn`` window mode.

Covers the search-result deep-link path: ``/graph/{id}?turn=N`` must return
the slice ``[N - window, N + window]`` regardless of ``max_chars``, so the
front-end source viewer renders the windowed conversation rather than
filtering an empty front-of-source slice.

Spec: bead auto-d1dvr.
"""

from __future__ import annotations

import pytest

from tools.graph import ops
from tools.graph.db import GraphDB
from tools.graph.models import Derivation, Source, Thought


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    return db_path


def _seed_long_session(db: GraphDB, *, turns: int = 50,
                       project: str = "autonomy") -> Source:
    """Seed a session with alternating user thoughts and assistant
    derivations spanning ``turns`` turn numbers. Each entry's content is
    long enough that a small ``max_chars`` cap will exclude later turns
    when ``around_turn`` is None."""
    src = Source(
        type="session",
        platform="claude-code",
        project=project,
        title="long session",
        file_path="session:long",
        metadata={"author": "test"},
    )
    db.insert_source(src)
    body = "x" * 800  # 800 chars per entry
    for n in range(1, turns + 1):
        if n % 2 == 1:
            db.insert_thought(Thought(
                source_id=src.id,
                content=f"user turn {n}: " + body,
                role="user",
                turn_number=n,
            ))
        else:
            db.insert_derivation(Derivation(
                source_id=src.id,
                content=f"assistant turn {n}: " + body,
                model="claude-opus",
                turn_number=n,
            ))
    db.conn.commit()
    return src


def test_read_source_full_default_returns_from_turn_1(graph_db_env):
    """Without ``around_turn``, the existing behaviour is preserved:
    entries start from turn 1 and the ``max_chars`` cap applies."""
    db = GraphDB(str(graph_db_env))
    src = _seed_long_session(db, turns=30)
    db.close()

    result = ops.read_source_full(src.id, max_chars=2000)
    assert result is not None
    entries = result["entries"]
    assert entries, "expected entries"
    # First entry must be turn 1 (no windowing → start from front).
    assert entries[0]["turn_number"] == 1
    # max_chars=2000 with ~800 char entries → only first ~3 entries fit.
    assert len(entries) < 30
    assert result["truncated"] is True


def test_read_source_full_default_carries_entry_created_at(graph_db_env):
    """Default full-source path must surface entry ``created_at`` for
    every thought and derivation — the source-viewer header metadata
    strip needs per-entry timestamps to render time range and duration.

    Pre-fix: ``db.get_source_content`` (used on the default path)
    omitted ``created_at`` from its SELECT, so every entry came back
    with ``created_at=None`` and the strip silently degraded. The
    ``around_turn`` and ``tail_n`` paths already selected the column,
    so context-mode views worked.

    Discovered during validation of auto-ptptn (header metadata strip).
    """
    db = GraphDB(str(graph_db_env))
    src = Source(
        type="session",
        platform="claude-code",
        project="autonomy",
        title="timestamped session",
        file_path="session:timestamped",
        metadata={"author": "test"},
    )
    db.insert_source(src)
    db.insert_thought(Thought(
        source_id=src.id,
        content="user opener",
        role="user",
        turn_number=1,
        created_at="2026-04-01T10:00:00Z",
    ))
    db.insert_derivation(Derivation(
        source_id=src.id,
        content="assistant reply",
        model="claude-opus",
        turn_number=2,
        created_at="2026-04-01T10:05:30Z",
    ))
    db.insert_thought(Thought(
        source_id=src.id,
        content="user follow-up",
        role="user",
        turn_number=3,
        created_at="2026-04-01T10:12:00Z",
    ))
    db.conn.commit()
    db.close()

    result = ops.read_source_full(src.id)
    assert result is not None
    entries = result["entries"]
    assert len(entries) == 3
    # Every entry must carry a non-null timestamp on the default path.
    timestamps = [e.get("created_at") for e in entries]
    assert all(ts is not None and ts != "" for ts in timestamps), (
        f"expected created_at for every entry, got {timestamps!r}"
    )
    # Both thought and derivation rows are covered (UNION ALL branches).
    assert timestamps == [
        "2026-04-01T10:00:00Z",
        "2026-04-01T10:05:30Z",
        "2026-04-01T10:12:00Z",
    ]


def test_read_source_full_with_around_turn_returns_window(graph_db_env):
    """With ``around_turn=N, window=W``, return entries with
    ``turn_number BETWEEN N - W AND N + W`` regardless of ``max_chars``."""
    db = GraphDB(str(graph_db_env))
    src = _seed_long_session(db, turns=50)
    db.close()

    # Pick a turn well past the max_chars cutoff (turn 25 of 50).
    result = ops.read_source_full(
        src.id, max_chars=2000, around_turn=25, window=5,
    )
    assert result is not None
    entries = result["entries"]
    turns = [e["turn_number"] for e in entries]
    # Window [20, 30] inclusive = 11 turns.
    assert turns == list(range(20, 31))
    # The max_chars cap must not truncate inside the window.
    for e in entries:
        assert len(e["content"]) >= 800


def test_read_source_full_window_includes_thoughts_and_derivations(graph_db_env):
    """Both thoughts (user) and derivations (assistant) appear in the
    windowed slice, ordered by turn_number."""
    db = GraphDB(str(graph_db_env))
    src = _seed_long_session(db, turns=20)
    db.close()

    result = ops.read_source_full(
        src.id, max_chars=2000, around_turn=10, window=3,
    )
    assert result is not None
    entries = result["entries"]
    roles = {e["role"] for e in entries}
    # Mixed roles confirm both sources are unioned.
    assert "user" in roles
    assert "claude-opus" in roles  # derivation.model becomes role
    turns = [e["turn_number"] for e in entries]
    assert turns == sorted(turns)
    assert min(turns) >= 7 and max(turns) <= 13


def test_read_source_full_window_clamps_at_source_bounds(graph_db_env):
    """``around_turn`` near turn 1 or near max returns a clamped slice
    without raising."""
    db = GraphDB(str(graph_db_env))
    src = _seed_long_session(db, turns=15)
    db.close()

    # Near front: window includes turn 1 even though lo would be -3.
    result = ops.read_source_full(src.id, around_turn=2, window=5)
    assert result is not None
    turns = [e["turn_number"] for e in result["entries"]]
    assert turns == list(range(1, 8))  # [max(1, -3) .. 7]

    # Near back: window includes turn 15 even though hi exceeds max.
    result = ops.read_source_full(src.id, around_turn=14, window=5)
    assert result is not None
    turns = [e["turn_number"] for e in result["entries"]]
    assert turns == list(range(9, 16))  # [9 .. min(15, 19)]

    # Past the end: empty window, no error.
    result = ops.read_source_full(src.id, around_turn=999, window=2)
    assert result is not None
    assert result["entries"] == []


def test_read_source_full_tail_n_returns_last_n_turns(graph_db_env):
    """``tail_n=N`` returns exactly the last N turns of the source.

    Locks in the ``?from=-N`` server-side resolution: callers ask for
    "the tail" without doing two round trips + JSON-string metadata
    parsing to find ``MAX(turn_number)`` first.
    """
    db = GraphDB(str(graph_db_env))
    src = _seed_long_session(db, turns=50)
    db.close()

    result = ops.read_source_full(src.id, max_chars=2000, tail_n=7)
    assert result is not None
    entries = result["entries"]
    turns = [e["turn_number"] for e in entries]
    assert turns == list(range(44, 51))
    # max_chars cap must not truncate inside the tail slice — same rule
    # as ``around_turn`` mode (live readers need complete trailing turns).
    for e in entries:
        assert len(e["content"]) >= 800


def test_read_source_full_tail_n_clamps_at_source_size(graph_db_env):
    """``tail_n`` larger than the total turn count returns every entry,
    not an error."""
    db = GraphDB(str(graph_db_env))
    src = _seed_long_session(db, turns=5)
    db.close()

    result = ops.read_source_full(src.id, tail_n=100)
    assert result is not None
    turns = [e["turn_number"] for e in result["entries"]]
    assert turns == [1, 2, 3, 4, 5]


def test_read_source_full_tail_n_bypasses_max_chars_cap(graph_db_env):
    """A 50-turn session at ~800 chars/turn exceeds the default 50K cap
    when read front-to-back (the legacy default truncates). ``tail_n``
    must skip the cap so live tails always render in full — that's the
    whole point of the bead, per ``a5134fd1-…`` (679 turns)."""
    db = GraphDB(str(graph_db_env))
    # 80 turns * ~800 chars > 50K — front-of-source read truncates.
    src = _seed_long_session(db, turns=80)
    db.close()

    # Front-of-source read truncates at default cap (sanity check).
    front = ops.read_source_full(src.id)
    assert front is not None
    assert front["truncated"] is True

    # Tail read does not truncate, even though total content > 50K.
    tail = ops.read_source_full(src.id, tail_n=20)
    assert tail is not None
    assert tail["truncated"] is False
    turns = [e["turn_number"] for e in tail["entries"]]
    assert turns == list(range(61, 81))
    for e in tail["entries"]:
        assert len(e["content"]) >= 800


def test_read_source_full_tail_n_empty_source(graph_db_env):
    """A source with no thoughts/derivations returns an empty list under
    ``tail_n`` — no SQL error from the MAX() resolver."""
    db = GraphDB(str(graph_db_env))
    src = Source(
        type="session",
        platform="claude-code",
        project="autonomy",
        title="empty",
        file_path="session:empty",
        metadata={},
    )
    db.insert_source(src)
    db.conn.commit()
    db.close()

    result = ops.read_source_full(src.id, tail_n=5)
    assert result is not None
    assert result["entries"] == []
