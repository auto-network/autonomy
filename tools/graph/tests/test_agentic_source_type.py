"""Tests for the ``agentic`` source type and its search-exclusion default
(bead auto-5k2j4).

Covers:

* ``ops.insert_agentic_session`` writes a typed source row with the full
  agent-action provenance metadata.
* ``GraphDB.search`` excludes ``agentic`` rows from every FTS entry point
  by default — project-scoped thoughts/derivations, global thoughts/
  derivations, and the ``_search_source_id`` FTS fallback.
* Passing ``excluded_source_types=[]`` opens the surface back up at every
  entry point (foundation for a future "Auxiliary runs" tab).
* ``list_notes`` is unaffected (already pinned to ``type='note'``).
* ``dao.sessions._session_group("agentic")`` and
  ``_session_group("agent-run")`` both fold into the ``dispatch`` bucket.
"""

from __future__ import annotations

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


def _seed_source_with_thought(
    db: GraphDB,
    *,
    type_: str,
    title: str,
    term: str,
    project: str = "autonomy",
    file_path: str | None = None,
    metadata: dict | None = None,
) -> Source:
    src = Source(
        type=type_,
        platform="local",
        title=title,
        file_path=file_path or f"{type_}:{title.replace(' ', '_')}",
        metadata=metadata or {"tags": ["test"]},
    )
    db.insert_source(src)
    db.insert_thought(Thought(
        source_id=src.id,
        content=f"discussion about {term}",
        role="user",
        turn_number=1,
        tags=["test"],
    ))
    db.conn.commit()
    return src


# ── helper: seed an agentic + a non-agentic source that both match the
# same FTS term; used by every search-exclusion test below.
def _seed_agentic_and_note(db: GraphDB, term: str, *, project: str = "autonomy"):
    note = _seed_source_with_thought(
        db, type_="note", title="note carrier", term=term, project=project,
    )
    agentic = _seed_source_with_thought(
        db,
        type_="agentic",
        title="agentic carrier",
        term=term,
        project=project,
        file_path=f"agentic:run-{term}",
        metadata={"kind": "agent-action", "member_key": "note.update-summary"},
    )
    return note, agentic


# ── insert_agentic_session ────────────────────────────────────


def test_insert_agentic_session_persists_metadata(graph_db_env):
    """The helper creates a row with type='agentic' and full provenance metadata."""
    # Touch the DB once so it exists before ops.insert_agentic_session opens it.
    GraphDB(str(graph_db_env)).close()

    row = ops.insert_agentic_session(
        org="autonomy",
        set_id="dashboard.agent-actions",
        set_revision=1,
        member_key="note.update-summary",
        model="claude-haiku-4-5-20251001",
        target_source_id="abc12345-aaaa-bbbb-cccc-ddddeeee0001",
        target_org="anchore",
        dispatched_by_session="session-uuid-7777",
        title="Update summary on abc12345",
    )

    assert row["type"] == "agentic"
    assert row["title"] == "Update summary on abc12345"
    assert row["org"] == "autonomy"
    assert row["slug"].startswith("agentic-update-summary-")

    meta = row["metadata"]
    assert meta["kind"] == "agent-action"
    assert meta["set_id"] == "dashboard.agent-actions"
    assert meta["set_revision"] == 1
    assert meta["member_key"] == "note.update-summary"
    assert meta["model"] == "claude-haiku-4-5-20251001"
    assert meta["target_kind"] == "source"
    assert meta["target_source_id"] == "abc12345-aaaa-bbbb-cccc-ddddeeee0001"
    assert meta["target_org"] == "anchore"
    assert meta["dispatched_by_session"] == "session-uuid-7777"
    assert meta["dispatched_at"], "dispatched_at must be a non-empty ISO timestamp"

    # Confirm it actually landed in the DB and is type 'agentic'.
    db = GraphDB(str(graph_db_env))
    try:
        persisted = db.get_source(row["id"])
    finally:
        db.close()
    assert persisted is not None
    assert persisted["type"] == "agentic"


def test_insert_agentic_session_persists_bead_identity(graph_db_env):
    GraphDB(str(graph_db_env)).close()

    row = ops.insert_agentic_session(
        org="autonomy",
        set_id="dashboard.agent-actions",
        set_revision=1,
        member_key="bead.dry-run-implement",
        model="claude-haiku-4-5-20251001",
        target_source_id="auto-bead-777",
        target_kind="bead",
        target_org="autonomy",
        dispatched_by_session="dashboard",
        title="Dry-Run Implement",
    )

    meta = row["metadata"]
    assert meta["target_kind"] == "bead"
    assert meta["target_source_id"] == "auto-bead-777"
    assert meta["target_org"] == "autonomy"


# ── search exclusion at every FTS entry point ─────────────────


def test_search_excludes_agentic_by_default_global_thoughts(graph_db_env):
    db = GraphDB(str(graph_db_env))
    note, agentic = _seed_agentic_and_note(db, "beta-token")
    db.close()

    # No project arg → global thought search path.
    results = ops.search("beta-token", include_raw=True)
    types = {r.get("source_type") for r in results}
    ids = {r.get("source_id") for r in results}

    assert "agentic" not in types
    assert agentic.id not in ids
    assert note.id in ids


def test_search_excludes_agentic_by_default_global_derivations(graph_db_env):
    db = GraphDB(str(graph_db_env))
    note = _seed_source_with_thought(
        db, type_="note", title="g-deriv note", term="deriv-token-gl",
    )
    agentic = Source(
        type="agentic",
        platform="local",
        title="g agentic carrier",
        file_path="agentic:gl-deriv",
        metadata={"kind": "agent-action"},
    )
    db.insert_source(agentic)
    from tools.graph.models import Derivation
    db.insert_derivation(Derivation(
        source_id=agentic.id,
        content="g-agentic derivation about deriv-token-gl",
        turn_number=1,
    ))
    db.insert_derivation(Derivation(
        source_id=note.id,
        content="g-note derivation about deriv-token-gl",
        turn_number=2,
    ))
    db.conn.commit()
    db.close()

    results = ops.search("deriv-token-gl", include_raw=True)
    types = {r.get("source_type") for r in results}
    ids = {r.get("source_id") for r in results}

    assert "agentic" not in types
    assert agentic.id not in ids


def test_search_excludes_agentic_in_search_source_id_fts_fallback(graph_db_env):
    """The FTS fallback inside _search_source_id must also drop agentic rows.

    We seed two sources whose THOUGHTS mention a target source ID; the
    direct prefix lookup for that ID returns nothing (the target row was
    never created), so the function falls through to its FTS fallback.
    The agentic carrier must be excluded from that fallback.
    """
    db = GraphDB(str(graph_db_env))
    # ID-shaped query string (12 hex chars = passes _is_source_id) that
    # matches no actual source row → forces FTS-fallback path.
    target_id = "deadbeef0042"

    note = _seed_source_with_thought(
        db, type_="note", title="mentioning note",
        term=target_id,
    )
    agentic = _seed_source_with_thought(
        db,
        type_="agentic",
        title="mentioning agentic",
        term=target_id,
        file_path="agentic:fallback",
        metadata={"kind": "agent-action"},
    )
    db.close()

    results = ops.search(target_id, include_raw=True)
    types = {r.get("source_type") for r in results}
    ids = {r.get("source_id") for r in results}

    assert "agentic" not in types
    assert agentic.id not in ids
    assert note.id in ids


# ── opt-in: passing excluded_source_types=[] surfaces agentic again ──


def test_search_includes_agentic_when_excluded_source_types_empty(graph_db_env):
    """``excluded_source_types=[]`` must surface agentic at EVERY entry point."""
    db = GraphDB(str(graph_db_env))
    note, agentic = _seed_agentic_and_note(db, "gamma-token")
    db.close()

    results = ops.search(
        "gamma-token", include_raw=True, excluded_source_types=[]
    )
    assert any(r.get("source_id") == agentic.id for r in results)


def test_search_includes_agentic_in_id_fallback_when_override(graph_db_env):
    """The FTS-fallback override must also surface agentic rows."""
    db = GraphDB(str(graph_db_env))
    target_id = "feedface0042"
    _seed_source_with_thought(
        db, type_="note", title="n-mention", term=target_id,
    )
    agentic = _seed_source_with_thought(
        db,
        type_="agentic",
        title="a-mention",
        term=target_id,
        file_path="agentic:fb",
        metadata={"kind": "agent-action"},
    )
    db.close()

    results = ops.search(
        target_id, include_raw=True, excluded_source_types=[]
    )
    assert any(r.get("source_id") == agentic.id for r in results)


# ── orthogonal surfaces are unaffected ────────────────────────


def test_list_notes_unaffected_by_agentic_default(graph_db_env):
    """list_notes already filters to type='note'; an agentic row must not leak in."""
    db = GraphDB(str(graph_db_env))
    note = _seed_source_with_thought(
        db, type_="note", title="real note", term="note-content",
    )
    agentic = _seed_source_with_thought(
        db,
        type_="agentic",
        title="agentic that should not surface",
        term="agentic-content",
        file_path="agentic:list-notes",
        metadata={"kind": "agent-action"},
    )
    db.close()

    notes = ops.list_notes(limit=50)
    note_ids = {n.get("id") for n in notes}

    assert note.id in note_ids
    assert agentic.id not in note_ids


def test_session_type_group_agentic_buckets_to_dispatch():
    """``_session_group`` (alias of _group_for_session_type) must route both
    'agentic' and 'agent-run' into the dispatch bucket. The default fallback
    is 'interactive', so an unmapped agentic value would silently land in
    the interactive bucket — the opposite of intent."""
    from tools.dashboard.dao import sessions as dao_sessions

    assert dao_sessions._session_group("agentic") == "dispatch"
    assert dao_sessions._session_group("agent-run") == "dispatch"
    # spot-check that existing buckets did not regress
    assert dao_sessions._session_group("interactive") == "interactive"
    assert dao_sessions._session_group("librarian") == "librarian"
    assert dao_sessions._session_group("dispatch") == "dispatch"
