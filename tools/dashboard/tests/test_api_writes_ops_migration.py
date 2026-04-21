"""Regression tests for the dashboard write-handler ops migration (auto-iv6c5).

Covers acceptance criterion #7 (no subprocess recursion) plus the
motivating regression auto-co51y (container POST /api/graph/note/update
must route by ``X-Graph-Org`` header — or more precisely, must not shell
out to a subprocess that lacks ``GRAPH_ORG``). Each handler:

* calls ``ops.*`` directly — verified by mocking ``run_cli`` to raise.
* honours the ``X-Graph-Org`` header for caller-org selection.
* returns ``409 cross_org_write_rejected`` when caller targets peer-origin
  content.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tools.graph import db as graph_db_mod
from tools.graph import ops
from tools.graph.db import GraphDB


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    GraphDB.close_all_pooled()
    try:
        yield root
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def dashboard_client(orgs_root, monkeypatch):
    """Boot the dashboard against the per-test orgs root.

    Fails the test if any handler attempts a graph-CLI subprocess —
    the whole point of this migration is that writes go through the
    in-process ops module.
    """
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    from tools.dashboard import server

    # Trip-wire: any residual subprocess shell-out to ``graph ...`` in a
    # write handler would reintroduce the co51y / o2vv9 class of bugs.
    original_run_cli = server.run_cli

    async def _run_cli_guard(cmd, *a, **kw):
        if cmd and cmd[0] == "graph":
            raise AssertionError(
                f"handler attempted to subprocess graph CLI: {cmd!r} "
                f"— it should call ops.* directly"
            )
        return await original_run_cli(cmd, *a, **kw)

    monkeypatch.setattr(server, "run_cli", _run_cli_guard)
    return TestClient(server.app)


def _make_peer_note(db_path: Path, *, title: str, state: str = "raw") -> str:
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


# ── Note create: X-Graph-Org routes to that DB ──────────────────


def test_api_graph_note_routes_by_x_graph_org(dashboard_client, orgs_root):
    resp = dashboard_client.post(
        "/api/graph/note",
        json={"content": "routed by header"},
        headers={"X-Graph-Org": "anchore"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["org"] == "anchore"

    # Verify it actually landed in anchore.db, not personal.db.
    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (body["source_id"],),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (body["source_id"],),
        ).fetchone()[0] == 0
    finally:
        ac.close()
        pc.close()


def test_api_graph_note_scopeless_lands_in_personal(dashboard_client, orgs_root):
    resp = dashboard_client.post(
        "/api/graph/note", json={"content": "scopeless"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True

    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert pc.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (body["source_id"],),
        ).fetchone()[0] == 1
    finally:
        pc.close()


# ── Note update: the motivating regression (auto-co51y) ─────────


def test_api_graph_note_update_auto_derives_to_home(dashboard_client, orgs_root):
    """Scopeless POST /api/graph/note/update for an autonomy-origin note
    must auto-derive the write target to autonomy.db, not reject."""
    # Set up an autonomy-origin note
    auto_id = _make_peer_note(orgs_root / "autonomy.db", title="autonomy-origin")

    resp = dashboard_client.post(
        "/api/graph/note/update",
        json={"source_id": auto_id, "content": "v2 from scopeless dashboard"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["source_id"] == auto_id
    assert body["new_version"] == 2

    au = sqlite3.connect(str(orgs_root / "autonomy.db"))
    try:
        row = au.execute(
            "SELECT version, content FROM note_versions WHERE source_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (auto_id,),
        ).fetchone()
        assert row == (2, "v2 from scopeless dashboard")
    finally:
        au.close()


def test_api_graph_note_update_rejects_cross_org(dashboard_client, orgs_root):
    """Explicit X-Graph-Org that does NOT match the note's origin → 409
    (the auto-co51y class of bug — container writes used to shell out
    and silently fail with 'not found' or write to the wrong DB)."""
    autonomy_id = _make_peer_note(
        orgs_root / "autonomy.db", title="autonomy-only",
    )

    resp = dashboard_client.post(
        "/api/graph/note/update",
        json={"source_id": autonomy_id, "content": "hijack attempt"},
        headers={"X-Graph-Org": "anchore"},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["target_id"] == autonomy_id
    assert body["origin_org"] == "autonomy"


# ── Note update: malformed source_id is still rejected at 400 ──


def test_api_graph_note_update_malformed_source_id(dashboard_client):
    resp = dashboard_client.post(
        "/api/graph/note/update",
        json={"source_id": "not an id!", "content": "x"},
    )
    assert resp.status_code == 400


# ── Comment handler migration smoke ─────────────────────────────


def test_api_graph_comment_lands_in_caller_org(dashboard_client, orgs_root):
    create = dashboard_client.post(
        "/api/graph/note",
        json={"content": "note to comment on"},
        headers={"X-Graph-Org": "anchore"},
    )
    assert create.status_code == 200
    note_id = create.json()["source_id"]

    resp = dashboard_client.post(
        "/api/graph/comment",
        json={"source_id": note_id, "content": "first comment"},
        headers={"X-Graph-Org": "anchore"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["source_id"] == note_id


def test_api_graph_comment_cross_org_rejected(dashboard_client, orgs_root):
    autonomy_id = _make_peer_note(
        orgs_root / "autonomy.db", title="autonomy thing",
    )
    resp = dashboard_client.post(
        "/api/graph/comment",
        json={"source_id": autonomy_id, "content": "peer comment attempt"},
        headers={"X-Graph-Org": "anchore"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["origin_org"] == "autonomy"


# ── Link handler migration ──────────────────────────────────────


def test_api_graph_link_creates_edge(dashboard_client, orgs_root):
    create = dashboard_client.post(
        "/api/graph/note",
        json={"content": "target for link"},
        headers={"X-Graph-Org": "anchore"},
    )
    note_id = create.json()["source_id"]

    resp = dashboard_client.post(
        "/api/graph/link",
        json={
            "bead_id": "auto-test-bead",
            "source_id": note_id,
            "relationship": "conceived_at",
            "turn": "5",
        },
        headers={"X-Graph-Org": "anchore"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["source_id"] == note_id
    assert body["bead_id"] == "auto-test-bead"

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    try:
        row = ac.execute(
            "SELECT COUNT(*) FROM edges WHERE id = ?", (body["edge_id"],),
        ).fetchone()
        assert row[0] == 1
    finally:
        ac.close()


# ── Attach handler migration ────────────────────────────────────


def test_api_graph_attach_stores_file(dashboard_client, orgs_root):
    resp = dashboard_client.post(
        "/api/graph/attach",
        files={"file": ("payload.txt", b"some bytes", "text/plain")},
        headers={"X-Graph-Org": "anchore"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["filename"] == "payload.txt"

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    try:
        row = ac.execute(
            "SELECT filename FROM attachments WHERE id = ?",
            (body["attachment_id"],),
        ).fetchone()
        assert row is not None
        assert row[0] == "payload.txt"
    finally:
        ac.close()


# ── Grep-level guarantee: no graph-CLI shell-outs left in server.py ──


def test_server_py_has_no_graph_cli_subprocess():
    """Acceptance criterion #1: zero run_cli(['graph', ...]) in server.py.

    A failure here would mean a future edit re-introduced a subprocess
    fork — likely reintroducing the auto-co51y / auto-o2vv9 bug class.
    """
    import re
    import tools.dashboard.server as server
    src = Path(server.__file__).read_text()
    # Match both ``run_cli(["graph", ...])`` and ``run_cli_json(["graph", ...])``
    # including when the call is split across lines (common after formatter).
    pattern = re.compile(
        r'run_cli(?:_json)?\s*\(\s*\n?\s*\[\s*"graph"', re.MULTILINE,
    )
    hits = pattern.findall(src)
    assert hits == [], (
        f"server.py must not subprocess graph CLI; found {len(hits)} sites: "
        f"{hits}"
    )
