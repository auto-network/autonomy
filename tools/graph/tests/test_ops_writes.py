"""Tests for the Phase-A ops.* write surface (auto-iv6c5).

Covers:

* ``ops.create_note`` — source + turn-1 thought land in caller org's DB,
  attachments get dedup'd, auto-provenance edge is inserted when wired.
* ``ops.update_note`` — version bump, comment integration, cross-org
  refusal via :class:`CrossOrgWriteError`.
* ``ops.attach_file`` — attachment row lands in caller org's DB, dedup
  by hash, cross-org refusal when target source is peer-origin.
* ``ops.create_edge`` — edge lands in caller org's DB regardless of
  target source's home (beads/edges are caller-owned).
* ``ops.read_source_full`` — cross-org read with own-org full surface.
* ``ops.stats`` / ``ops.get_context`` — smoke coverage.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from tools.graph import cli as graph_cli
from tools.graph import db as graph_db_mod
from tools.graph import ops
from tools.graph.db import GraphDB


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    # Cross-org peer-DB reads flow through the process-lifetime pool
    # (``GraphDB.for_org``); clear it so a prior test's cached handle
    # to a now-deleted tmp path can't shadow this test's new org DBs.
    GraphDB.close_all_pooled()
    try:
        yield root
    finally:
        GraphDB.close_all_pooled()


def _make_peer_note(db_path: Path, *, title: str, state: str = "raw") -> str:
    """Seed a note in ``db_path`` and return its id. Used to set up
    peer-origin rows for cross-org refusal tests."""
    from tools.graph.models import Source, Thought
    db = GraphDB(db_path)
    try:
        src = Source(
            type="note", platform="local", project="autonomy",
            title=title, file_path=f"note:{title.replace(' ', '_')}",
            metadata={"tags": [], "author": "test"},
            publication_state=state,
        )
        db.insert_source(src)
        db.insert_thought(Thought(
            source_id=src.id, content=title, role="user", turn_number=1,
        ))
        db.insert_note_version(src.id, 1, title)
        db.commit()
        return src.id
    finally:
        db.close()


# ── create_note ──────────────────────────────────────────────


def test_create_note_lands_in_caller_org(orgs_root):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    result = ops.create_note(
        "hello anchore\nline two", tags=["pitfall"], author="pytest",
        org="anchore",
    )

    assert result["org"] == "anchore"
    assert result["lines"] == 2
    assert result["chars"] == len("hello anchore\nline two")
    assert result["source_id"] == result["id"]

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (result["id"],),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (result["id"],),
        ).fetchone()[0] == 0
        row = ac.execute(
            "SELECT content FROM thoughts WHERE source_id = ? AND turn_number = 1",
            (result["id"],),
        ).fetchone()
        assert row[0] == "hello anchore\nline two"
    finally:
        ac.close()
        pc.close()


def test_create_note_with_attachment_dedup(orgs_root, tmp_path):
    GraphDB.create_org_db("personal", type_="personal").close()

    f = tmp_path / "shot.png"
    f.write_bytes(b"PNG-DATA")

    r = ops.create_note("attach test {1}", attachments=[str(f)])
    atts = r["attachments"]
    assert len(atts) == 1
    # Placeholder substitution worked (content now references graph://<id>)
    assert "graph://" in r["content"]
    assert f"graph://{atts[0]['id'][:12]}" in r["content"]

    # Second note, same file → same attachment id (hash-dedup)
    r2 = ops.create_note("another {1}", attachments=[str(f)])
    assert r2["attachments"][0]["id"] == atts[0]["id"]


def test_create_note_with_provenance_edge(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()

    peer_src = _make_peer_note(orgs_root / "personal.db", title="session-src")

    r = ops.create_note(
        "provenanced",
        auto_provenance_source_id=peer_src,
        auto_provenance_turn=5,
    )
    assert r["auto_provenance"] == {"source_id": peer_src, "turn": 5}

    # Verify the edge exists in personal.db (caller's own DB)
    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        edge_row = conn.execute(
            "SELECT relation, target_id FROM edges WHERE source_id = ?",
            (r["id"],),
        ).fetchone()
        assert edge_row is not None
        assert edge_row[0] == "conceived_at"
        assert edge_row[1] == peer_src
    finally:
        conn.close()


# ── update_note ──────────────────────────────────────────────


def test_update_note_bumps_version(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()

    r = ops.create_note("v1 content")
    upd = ops.update_note(r["id"], "v2 content")
    assert upd["new_version"] == 2
    assert upd["source_id"] == r["id"]
    assert upd["content"] == "v2 content"

    # Re-fetch to confirm thought + title updated to v2.
    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        row = conn.execute(
            "SELECT title FROM sources WHERE id = ?", (r["id"],),
        ).fetchone()
        assert row[0] == "v2 content"
        row = conn.execute(
            "SELECT content FROM thoughts WHERE source_id = ? AND turn_number = 1",
            (r["id"],),
        ).fetchone()
        assert row[0] == "v2 content"
    finally:
        conn.close()


def test_update_note_integrates_comments(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()

    r = ops.create_note("comment target")
    comment = ops.add_comment(r["id"], "fix step 3")

    upd = ops.update_note(
        r["id"], "updated content", integrate_comments=[comment["id"]],
    )
    assert upd["integrated"] == [comment["id"]]

    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        row = conn.execute(
            "SELECT integrated FROM note_comments WHERE id = ?",
            (comment["id"],),
        ).fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_update_note_refuses_peer_origin(orgs_root):
    """The motivating regression for auto-iv6c5: caller-scoped session
    attempts to update a peer-origin note. Must raise
    :class:`CrossOrgWriteError` instead of silently creating a new
    version in caller's DB."""
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    # Peer (anchore) has a published note; autonomy session tries to update it.
    peer_id = _make_peer_note(
        orgs_root / "anchore.db", title="anchore published",
        state="published",
    )

    with pytest.raises(ops.CrossOrgWriteError) as exc:
        ops.update_note(peer_id, "autonomy hijack", org="autonomy")
    assert exc.value.origin_org == "anchore"
    assert exc.value.target_id == peer_id


def test_update_note_not_found_raises_lookup(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()
    with pytest.raises(LookupError):
        ops.update_note("nonexistent-id", "anything")


# ── attach_file ──────────────────────────────────────────────


def test_attach_file_lands_in_caller_org(orgs_root, tmp_path):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    f = tmp_path / "diag.txt"
    f.write_text("diagnostic payload")

    att = ops.attach_file(str(f), org="anchore")
    assert att["filename"] == "diag.txt"
    assert att["size_bytes"] == len("diagnostic payload")

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM attachments WHERE id = ?", (att["id"],),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM attachments WHERE id = ?", (att["id"],),
        ).fetchone()[0] == 0
        row = ac.execute(
            "SELECT file_path FROM attachments WHERE id = ?", (att["id"],),
        ).fetchone()
        assert row is not None
        assert "/orgs/attachments/" not in row[0]
    finally:
        ac.close()
        pc.close()


def test_attach_file_refuses_peer_source(orgs_root, tmp_path):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    peer_id = _make_peer_note(
        orgs_root / "anchore.db", title="peer note",
    )
    f = tmp_path / "peer.jpg"
    f.write_bytes(b"JPG")

    with pytest.raises(ops.CrossOrgWriteError):
        ops.attach_file(str(f), source_id=peer_id, org="autonomy")


def test_attach_file_missing_raises(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()
    with pytest.raises(FileNotFoundError):
        ops.attach_file("/nonexistent/path.png")


def test_move_source_copies_live_row_and_leaves_origin_stub(orgs_root, tmp_path):
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    f = tmp_path / "move.txt"
    f.write_text("move attachment payload")
    moved = ops.create_note("move me {1}", attachments=[str(f)], org="personal")

    target_id = _make_peer_note(
        orgs_root / "personal.db", title="move target",
    )
    ops.create_edge(
        moved["source_id"], target_id,
        from_type="source", to_type="source", relation="conceived_at",
        org="personal",
    )
    ops.create_edge(
        "auto-move-bead", moved["source_id"],
        from_type="bead", to_type="source", relation="informed_by",
        org="personal",
    )

    result = ops.move_source(
        moved["source_id"], "personal", "autonomy", reason="org transfer",
    )

    assert result["source_id"] == moved["source_id"]
    assert result["from_org"] == "personal"
    assert result["to_org"] == "autonomy"

    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    ac = sqlite3.connect(str(orgs_root / "autonomy.db"))
    pc.row_factory = sqlite3.Row
    ac.row_factory = sqlite3.Row
    try:
        prow = pc.execute(
            "SELECT deprecated, moved_to_org, metadata FROM sources WHERE id = ?",
            (moved["source_id"],),
        ).fetchone()
        assert prow is not None
        assert prow["deprecated"] == 1
        assert prow["moved_to_org"] == "autonomy"
        pmeta = json.loads(prow["metadata"] or "{}")
        assert pmeta["moved"]["at"]
        assert pmeta["moved"]["reason"] == "org transfer"

        arow = ac.execute(
            "SELECT deprecated, moved_to_org FROM sources WHERE id = ?",
            (moved["source_id"],),
        ).fetchone()
        assert arow is not None
        assert arow["deprecated"] == 0
        assert arow["moved_to_org"] is None

        assert pc.execute(
            "SELECT COUNT(*) FROM thoughts WHERE source_id = ?",
            (moved["source_id"],),
        ).fetchone()[0] == 0
        assert ac.execute(
            "SELECT COUNT(*) FROM thoughts WHERE source_id = ?",
            (moved["source_id"],),
        ).fetchone()[0] == 1

        assert pc.execute(
            "SELECT COUNT(*) FROM note_versions WHERE source_id = ?",
            (moved["source_id"],),
        ).fetchone()[0] == 0
        assert ac.execute(
            "SELECT COUNT(*) FROM note_versions WHERE source_id = ?",
            (moved["source_id"],),
        ).fetchone()[0] == 1

        assert pc.execute(
            "SELECT COUNT(*) FROM attachments WHERE source_id = ? OR source_id LIKE ?",
            (moved["source_id"], f"{moved['source_id']}@%"),
        ).fetchone()[0] == 0
        assert ac.execute(
            "SELECT COUNT(*) FROM attachments WHERE source_id = ? OR source_id LIKE ?",
            (moved["source_id"], f"{moved['source_id']}@%"),
        ).fetchone()[0] == 1

        assert pc.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ?",
            (moved["source_id"],),
        ).fetchone()[0] == 0
        assert ac.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ?",
            (moved["source_id"],),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM edges WHERE target_id = ? AND source_id = 'auto-move-bead'",
            (moved["source_id"],),
        ).fetchone()[0] == 1
    finally:
        pc.close()
        ac.close()


def test_cmd_move_uses_explicit_from_org_even_when_graph_org_differs(orgs_root):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    created = ops.create_note("cli move explicit from", org="personal")

    args = argparse.Namespace(
        db=graph_db_mod.resolve_caller_db_path(None),
        source_id=created["source_id"][:12],
        from_org="personal",
        to_org="autonomy",
        reason="explicit from test",
    )

    from pytest import MonkeyPatch
    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("GRAPH_ORG", "anchore")
    try:
        graph_cli.cmd_move(args)
    finally:
        monkeypatch.undo()

    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    ac = sqlite3.connect(str(orgs_root / "autonomy.db"))
    try:
        prow = pc.execute(
            "SELECT moved_to_org, deprecated FROM sources WHERE id = ?",
            (created["source_id"],),
        ).fetchone()
        arow = ac.execute(
            "SELECT moved_to_org, deprecated FROM sources WHERE id = ?",
            (created["source_id"],),
        ).fetchone()
        assert prow == ("autonomy", 1)
        assert arow == (None, 0)
    finally:
        pc.close()
        ac.close()


# ── create_edge ──────────────────────────────────────────────


def test_create_edge_lands_in_caller_own_db(orgs_root):
    """Edges are caller-owned. Even when the target source lives in a
    peer DB, the edge row lands in caller's DB."""
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    peer_id = _make_peer_note(
        orgs_root / "autonomy.db", title="autonomy target",
        state="published",
    )

    edge = ops.create_edge(
        "auto-xyz9", peer_id,
        from_type="bead", to_type="source",
        relation="conceived_at",
        turns=(10, 10),
        org="anchore",
    )
    assert edge["source_id"] == "auto-xyz9"
    assert edge["target_id"] == peer_id

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    au = sqlite3.connect(str(orgs_root / "autonomy.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM edges WHERE id = ?", (edge["id"],),
        ).fetchone()[0] == 1
        # Autonomy must NOT see the edge — it's a caller-owned artifact.
        assert au.execute(
            "SELECT COUNT(*) FROM edges WHERE id = ?", (edge["id"],),
        ).fetchone()[0] == 0
    finally:
        ac.close()
        au.close()


# ── read_source_full ─────────────────────────────────────────


def test_read_source_full_own_org(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()

    r = ops.create_note("full read test content")
    payload = ops.read_source_full(r["id"], max_chars=100)
    assert payload is not None
    assert payload["source"]["id"] == r["id"]
    assert payload["total_chars"] > 0
    assert len(payload["entries"]) >= 1


def test_read_source_full_peer_public_surface(orgs_root):
    """A caller session can read a peer-origin source marked
    ``published``/``canonical`` via ``read_source_full`` — the helper
    follows the same cross-org read semantics as ``get_source``."""
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    peer_id = _make_peer_note(
        orgs_root / "autonomy.db", title="public autonomy note",
        state="published",
    )

    payload = ops.read_source_full(peer_id, org="anchore")
    assert payload is not None
    assert payload["source"]["id"] == peer_id
    assert payload["source"]["org"] == "autonomy"


def test_read_source_full_missing_returns_none(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()
    assert ops.read_source_full("nonexistent") is None


# ── stats / get_context smoke ─────────────────────────────────


def test_stats_returns_dict(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()
    data = ops.stats()
    assert isinstance(data, dict)
    # Known tables — safe even on a fresh DB
    assert "sources" in data or "thoughts" in data or data == {}


def test_get_context_own_org(orgs_root):
    GraphDB.create_org_db("personal", type_="personal").close()
    r = ops.create_note("turn content")
    ctx = ops.get_context(r["id"], 1, window=1)
    assert ctx is not None
    assert ctx["source"]["id"] == r["id"]
    assert ctx["center_turn"] == 1
    assert any(t["turn_number"] == 1 for t in ctx["turns"])


# ── GRAPH_ORG env wiring ──────────────────────────────────────


def test_create_note_follows_graph_org_env(orgs_root, monkeypatch):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    monkeypatch.setenv("GRAPH_ORG", "anchore")

    r = ops.create_note("env-routed note")
    assert r["org"] == "anchore"

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (r["id"],),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (r["id"],),
        ).fetchone()[0] == 0
    finally:
        ac.close()
        pc.close()
