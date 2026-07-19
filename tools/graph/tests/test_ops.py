"""Unit tests for ``tools.graph.ops``.

Each test exercises one ops function against a real ephemeral GraphDB
(SQLite is fast enough that mocking adds noise without speed). The mocking
focus here is environment isolation — each test pins ``GRAPH_DB`` to its
own tmp file so concurrent runs cannot collide.
"""

from __future__ import annotations

import json
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


def test_create_note_session_hint_resolves_conceived_at_edge(graph_db_env):
    """create_note(session_hint=...) links the note to the caller's session + turn.

    This is the server-side path that replaces cli._auto_provenance for
    HttpClient/container callers, who have no local sqlite mirror to
    resolve provenance against themselves.
    """
    db = GraphDB(str(graph_db_env))
    session = Source(
        type="session", platform="claude-code",
        title="a session",
        metadata={"session_id": "auto-0322-153000"},
    )
    db.insert_source(session)
    db.insert_thought(Thought(
        source_id=session.id, content="let's talk about passkey enrollment",
        role="user", turn_number=1,
    ))
    db.insert_thought(Thought(
        source_id=session.id, content="unrelated later turn",
        role="user", turn_number=2,
    ))
    db.commit()
    db.close()

    result = ops.create_note(
        "passkey enrollment notes", tags=[], session_hint="auto-0322-153000",
    )
    assert result["auto_provenance"] == {"source_id": session.id, "turn": 1}

    db2 = GraphDB(str(graph_db_env))
    edge = db2.conn.execute(
        "SELECT * FROM edges WHERE source_id = ? AND relation = 'conceived_at'",
        (result["source_id"],),
    ).fetchone()
    db2.close()
    assert edge is not None
    assert edge["target_id"] == session.id


def test_create_note_session_hint_unknown_session_is_noop(graph_db_env):
    """An unresolvable session_hint doesn't fail note creation, just skips the edge."""
    GraphDB(str(graph_db_env)).close()
    result = ops.create_note(
        "orphan note", tags=[], session_hint="auto-does-not-exist",
    )
    assert result["auto_provenance"] is None


def test_query_attention_includes_claude_platform_sessions(graph_db_env):
    """platform='claude' (the interactive container-agent path, live since
    2026-07-03) must not be excluded from attention.

    A prior ``s.platform IN ('claude-code', 'codex-cli', 'codex-tui')``
    allowlist silently dropped every platform='claude' session — the
    session_type check already scopes correctly to human-present sessions,
    so the platform condition was a redundant, over-narrow proxy.
    """
    db = GraphDB(str(graph_db_env))
    session = Source(
        type="session", platform="claude",
        title="an interactive container session",
        metadata={"session_id": "auto-0719-124323", "session_type": "terminal"},
    )
    db.insert_source(session)
    db.insert_thought(Thought(
        source_id=session.id, content="please check the backfill approval",
        role="user", turn_number=1,
    ))
    db.commit()
    db.close()

    db2 = GraphDB(str(graph_db_env))
    rows = ops._query_attention(db2, session="auto-0719-124323")
    db2.close()

    assert len(rows) == 1
    assert rows[0]["content"] == "please check the backfill approval"


def test_add_tag_returns_true_on_first_application(graph_db_env):
    """Tag is newly added on first call, no-op on second."""
    db = GraphDB(str(graph_db_env))
    src = _seed_note(db, title="taggable", tags=[])
    db.close()

    assert ops.add_tag(src.id, "shiny") is True
    assert ops.add_tag(src.id, "shiny") is False


def _fake_locate(note_id_to_org: dict):
    def _locate(source_id, *, org=None):
        if source_id not in note_id_to_org:
            return None
        return {"org": note_id_to_org[source_id], "id": source_id, "type": "note"}
    return _locate


def test_backfill_note_metadata_dry_run_does_not_write(graph_db_env, monkeypatch):
    """dry_run=True (the default) reports the diff without touching the DB."""
    db = GraphDB(str(graph_db_env))
    note = _seed_note(db, title="orphan note", tags=[])
    db.close()

    monkeypatch.setattr(ops, "locate_source_org", _fake_locate({note.id: "autonomy"}))

    result = ops.backfill_note_metadata(
        [{"id": note.id, "value": "auto-0322-153000"}], field="author",
    )
    assert result["dry_run"] is True
    assert result["applied"] == 0
    assert result["changes"] == [
        {"id": note.id, "org": "autonomy", "before": "test", "after": "auto-0322-153000"},
    ]

    reread = ops.get_source(note.id)
    meta = reread["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta.get("author") == "test"  # unchanged


def test_backfill_note_metadata_commits_scalar_field(graph_db_env, monkeypatch):
    """dry_run=False patches metadata.author on exactly the named note."""
    db = GraphDB(str(graph_db_env))
    note = _seed_note(db, title="orphan note", tags=[])
    other = _seed_note(db, title="untouched note", tags=[])
    db.close()

    monkeypatch.setattr(
        ops, "locate_source_org",
        _fake_locate({note.id: "autonomy", other.id: "autonomy"}),
    )

    result = ops.backfill_note_metadata(
        [{"id": note.id, "value": "auto-0322-153000"}], field="author", dry_run=False,
    )
    assert result["applied"] == 1
    assert result["skipped"] == []

    patched = ops.get_source(note.id)
    meta = patched["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta["author"] == "auto-0322-153000"
    assert meta["tags"] == []  # other metadata keys survive the json_set patch

    untouched = ops.get_source(other.id)
    umeta = untouched["metadata"]
    if isinstance(umeta, str):
        umeta = json.loads(umeta)
    assert umeta["author"] == "test"


def test_backfill_note_metadata_commits_object_field(graph_db_env, monkeypatch):
    """dry_run=False patches an object-valued field (metadata.identity) correctly."""
    db = GraphDB(str(graph_db_env))
    note = _seed_note(db, title="orphan note", tags=[])
    db.close()

    monkeypatch.setattr(ops, "locate_source_org", _fake_locate({note.id: "autonomy"}))

    identity_value = {"root_pub": "be2afef6", "attested": "operator-backfill-2026-07-19"}
    result = ops.backfill_note_metadata(
        [{"id": note.id, "value": identity_value}], field="identity", dry_run=False,
    )
    assert result["applied"] == 1

    patched = ops.get_source(note.id)
    meta = patched["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta["identity"] == identity_value


def test_backfill_note_metadata_rejects_unknown_field(graph_db_env):
    """Only the explicit allowlist (author, identity) is patchable."""
    with pytest.raises(ValueError):
        ops.backfill_note_metadata([{"id": "whatever", "value": "x"}], field="title")


def test_backfill_note_metadata_skips_non_note_and_missing(graph_db_env, monkeypatch):
    """Non-note sources and unresolvable ids are skipped, not silently patched."""
    monkeypatch.setattr(
        ops, "locate_source_org",
        lambda source_id, org=None: (
            {"org": "autonomy", "id": "a-session-id", "type": "session"}
            if source_id == "a-session-id" else None
        ),
    )
    result = ops.backfill_note_metadata(
        [
            {"id": "a-session-id", "value": "x"},
            {"id": "does-not-exist", "value": "x"},
        ],
        field="author",
    )
    assert result["applied"] == 0
    assert result["changes"] == []
    reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert "not a note" in reasons["a-session-id"]
    assert "not found" in reasons["does-not-exist"]


def test_backfill_note_provenance_dry_run_does_not_write(graph_db_env, monkeypatch):
    """dry_run=True (the default) plans the edge without creating it."""
    db = GraphDB(str(graph_db_env))
    note = _seed_note(db, title="orphan note", tags=[])
    session = Source(type="session", platform="claude", title="s")
    db.insert_source(session)
    db.close()

    monkeypatch.setattr(ops, "locate_source_org", _fake_locate({note.id: "autonomy"}))

    result = ops.backfill_note_provenance(
        [{"id": note.id, "session_source_id": session.id, "turn": 4, "confidence": 0.95}],
    )
    assert result["dry_run"] is True
    assert result["applied"] == 0
    assert result["changes"] == [
        {"id": note.id, "org": "autonomy", "session_source_id": session.id,
         "turn": 4, "confidence": 0.95},
    ]

    db2 = GraphDB(str(graph_db_env))
    edge = db2.conn.execute(
        "SELECT * FROM edges WHERE source_id = ? AND relation = 'conceived_at'",
        (note.id,),
    ).fetchone()
    db2.close()
    assert edge is None


def test_backfill_note_provenance_commits_edge(graph_db_env, monkeypatch):
    """dry_run=False creates a conceived_at edge with turn + confidence metadata."""
    db = GraphDB(str(graph_db_env))
    note = _seed_note(db, title="orphan note", tags=[])
    session = Source(type="session", platform="claude", title="s")
    db.insert_source(session)
    db.close()

    monkeypatch.setattr(ops, "locate_source_org", _fake_locate({note.id: "autonomy"}))

    result = ops.backfill_note_provenance(
        [{"id": note.id, "session_source_id": session.id, "turn": 4, "confidence": 0.95}],
        dry_run=False,
    )
    assert result["applied"] == 1

    db2 = GraphDB(str(graph_db_env))
    edge = db2.conn.execute(
        "SELECT * FROM edges WHERE source_id = ? AND relation = 'conceived_at'",
        (note.id,),
    ).fetchone()
    db2.close()
    assert edge is not None
    assert edge["target_id"] == session.id
    meta = json.loads(edge["metadata"])
    assert meta["turns"] == {"from": 4, "to": 4}
    assert meta["confidence"] == 0.95


def test_backfill_note_provenance_skips_existing_edge(graph_db_env, monkeypatch):
    """A note that already has a conceived_at edge is skipped, never overwritten."""
    db = GraphDB(str(graph_db_env))
    note = _seed_note(db, title="already linked", tags=[])
    session_a = Source(type="session", platform="claude", title="a")
    session_b = Source(type="session", platform="claude", title="b")
    db.insert_source(session_a)
    db.insert_source(session_b)
    db.close()

    # Simulate the going-forward fix already having created a correct edge.
    live_result = ops.create_note(
        "placeholder", tags=[], auto_provenance_source_id=session_a.id,
        auto_provenance_turn=1,
    )
    note_with_edge = live_result["source_id"]

    monkeypatch.setattr(
        ops, "locate_source_org", _fake_locate({note_with_edge: "autonomy"}),
    )

    result = ops.backfill_note_provenance(
        [{"id": note_with_edge, "session_source_id": session_b.id, "turn": 9, "confidence": 0.5}],
        dry_run=False,
    )
    assert result["applied"] == 0
    assert result["skipped"] == [
        {"id": note_with_edge, "reason": "already has a conceived_at edge"},
    ]

    db3 = GraphDB(str(graph_db_env))
    edge = db3.conn.execute(
        "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'conceived_at'",
        (note_with_edge,),
    ).fetchone()
    db3.close()
    assert edge["target_id"] == session_a.id  # untouched, not overwritten by session_b


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


def test_org_kwarg_accepted(graph_db_env):
    """org and peers parameters are accepted (placeholder for per-org DB).

    Today they are ignored — verify the signatures accept them without error
    so downstream beads can pass them through call sites.
    """
    GraphDB(str(graph_db_env)).close()
    # Should not raise
    ops.search("anything", org="autonomy", peers=["anchore"])
    ops.list_sources(org="autonomy", peers=["anchore"], limit=1)
    ops.get_source("missing", org="autonomy", peers=None)


def _seed_and_commit(db: GraphDB, *, title: str, tags: list[str],
                     project: str = "autonomy") -> Source:
    """Seed a note and force a commit (``insert_thought`` does not commit
    on its own; pending rows would be lost on close otherwise)."""
    src = _seed_note(db, title=title, tags=tags, project=project)
    db.conn.commit()
    return src


def test_org_none_defaults_to_personal(tmp_path, monkeypatch):
    """With per-org DBs present and no GRAPH_DB override, ``org=None``
    resolves to ``personal`` — writes/reads land in ``personal.db``
    (auto-txg5.3 scopeless convergence, absorbing auto-s45z9)."""
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    personal = GraphDB.create_org_db("personal", type_="personal")
    try:
        _seed_and_commit(personal, title="default-routed note", tags=["routing"])
    finally:
        personal.close()

    results = ops.search("default-routed", include_raw=True)
    assert any(
        "default-routed" in (r.get("content") or "").lower()
        or "default-routed" in (r.get("source_title") or "").lower()
        for r in results
    )


def test_org_routes_to_specific_org_db(tmp_path, monkeypatch):
    """Different org values open different per-org DBs."""
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    for slug in ("autonomy", "anchore"):
        db = GraphDB.create_org_db(slug)
        try:
            _seed_and_commit(db, title=f"lives in {slug}", tags=["x"])
        finally:
            db.close()

    aut_hits = ops.search("autonomy", org="autonomy", include_raw=True)
    anc_hits = ops.search("anchore", org="anchore", include_raw=True)

    def titles(rs):
        return {r.get("source_title") or r.get("content") for r in rs}

    assert any("lives in autonomy" in (t or "").lower() for t in titles(aut_hits))
    assert any("lives in anchore" in (t or "").lower() for t in titles(anc_hits))
    # Autonomy caller must NOT see the anchore-only note.
    assert not any("anchore" in (t or "").lower() for t in titles(aut_hits))


def test_legacy_graph_db_still_openable(tmp_path, monkeypatch):
    """Pre-migration deployments with no data/orgs/ still work.

    When org resolves to autonomy and autonomy.db is absent, we
    fall back to the ``GRAPH_DB`` env override (and further to the legacy
    ``data/graph.db``). Existing tests use this fallback via the
    ``graph_db_env`` fixture.
    """
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "absent_orgs"))
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("GRAPH_DB", str(legacy))
    monkeypatch.delenv("GRAPH_API", raising=False)
    db = GraphDB(str(legacy))
    _seed_and_commit(db, title="legacy routed", tags=["legacy"])
    db.close()

    results = ops.search("legacy", include_raw=True)
    assert any(
        "legacy" in (r.get("source_title") or "").lower()
        for r in results
    )
