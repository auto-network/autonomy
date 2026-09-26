"""move_source across fleet-synced and plain orgs (auto-rfets).

The move used to ATTACH the target to the origin's connection and write
``target.<table>`` / ``main.<table>``. The fleet-sync hook reads the table
name from the statement, so those schema-qualified writes opened no
authored context and every combination involving a synced org was refused
by the fail-closed capture triggers (an opaque "user-defined function raised
exception"). Each org's rows are now written through that org's own store.
"""
from __future__ import annotations

import sqlite3

import pytest

from tools.graph import ops
from tools.graph.db import GraphDB

ORIGIN_INC = "a" * 64
TARGET_INC = "c" * 64


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    try:
        yield root
    finally:
        GraphDB.close_all_pooled()


def _orgs(root, *, origin_synced: bool, target_synced: bool):
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    origin, target = root.parent / "personal.db", root / "autonomy.db"
    if origin_synced:
        with GraphDB(origin) as db:
            db.activate_fleet_sync_writers(ORIGIN_INC)
    if target_synced:
        with GraphDB(target) as db:
            db.activate_fleet_sync_writers(TARGET_INC)
    return origin, target


def _note(tmp_path) -> str:
    f = tmp_path / "payload.txt"
    f.write_text("attachment payload")
    sid = ops.create_note("move me {1}", attachments=[str(f)], org="personal")["source_id"]
    ops.update_note(sid, "move me, revised {1}", org="personal")
    GraphDB.close_all_pooled()
    return sid


def _served(path, incarnation) -> list[tuple[str, str, bool]]:
    """(table, address, tombstone) the store would serve for its own origin."""
    with GraphDB(path) as db:
        catalog = db._fleet_catalog
        assert catalog is not None
        out = []
        for _, _, _, items in catalog.next_transactions_for_origin(
                incarnation, 0, limit=100000):
            for m in items:
                assert m.origin_incarnation == incarnation
                out.append((m.mutation.table, str(m.mutation.address),
                            m.mutation.tombstone))
        return out


def _count(path, table, sid) -> int:
    c = sqlite3.connect(path)
    try:
        return c.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source_id = ?", (sid,)
        ).fetchone()[0]
    finally:
        c.close()


@pytest.mark.parametrize("origin_synced", [False, True])
@pytest.mark.parametrize("target_synced", [False, True])
def test_move_is_captured_by_each_store(orgs_root, tmp_path, origin_synced, target_synced):
    origin, target = _orgs(orgs_root, origin_synced=origin_synced,
                           target_synced=target_synced)
    sid = _note(tmp_path)
    before = {t: _count(origin, t, sid) for t in ("note_versions", "attachments")}
    assert before["note_versions"] >= 1 and before["attachments"] == 1

    ops.move_source(sid, "personal", "autonomy", reason="probe")
    GraphDB.close_all_pooled()

    for table, n in before.items():
        assert _count(target, table, sid) == n
        assert _count(origin, table, sid) == 0
    c = sqlite3.connect(origin)
    assert c.execute("SELECT moved_to_org FROM sources WHERE id = ?",
                     (sid,)).fetchone()[0] == "autonomy"
    c.close()

    if target_synced:
        served = _served(target, TARGET_INC)
        tables = {t for t, addr, tomb in served if sid in addr and not tomb}
        assert {"sources", "note_versions"} <= tables
        # Attachments are addressed by their own id, not the note's.
        t = sqlite3.connect(target)
        (att_id,) = t.execute("SELECT id FROM attachments WHERE source_id = ?",
                              (sid,)).fetchone()
        t.close()
        assert any(tb == "attachments" and att_id in a and not tomb
                   for tb, a, tomb in served)
    if origin_synced:
        served = _served(origin, ORIGIN_INC)
        assert any(t == "note_versions" and sid in a and tomb for t, a, tomb in served)
        assert any(t == "sources" and sid in a and not tomb for t, a, tomb in served)


def test_interrupted_move_completes_on_rerun(orgs_root, tmp_path):
    origin, target = _orgs(orgs_root, origin_synced=False, target_synced=True)
    sid = _note(tmp_path)
    versions = _count(origin, "note_versions", sid)

    # Fail the origin phase after the target copy has committed.
    c = sqlite3.connect(origin)
    c.execute(
        "CREATE TRIGGER boom BEFORE UPDATE OF moved_to_org ON sources "
        "WHEN NEW.moved_to_org IS NOT NULL BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    c.commit()
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        ops.move_source(sid, "personal", "autonomy")
    GraphDB.close_all_pooled()
    assert _count(target, "note_versions", sid) == versions
    assert _count(origin, "note_versions", sid) == versions   # origin untouched
    c.execute("DROP TRIGGER boom")
    c.commit()
    c.close()

    ops.move_source(sid, "personal", "autonomy")
    GraphDB.close_all_pooled()
    assert _count(target, "note_versions", sid) == versions
    assert _count(origin, "note_versions", sid) == 0
    t = sqlite3.connect(target)
    assert t.execute("SELECT COUNT(*) FROM sources WHERE id = ?", (sid,)).fetchone()[0] == 1
    t.close()


def test_note_version_authorship_moves_with_the_version(orgs_root, tmp_path):
    origin, target = _orgs(orgs_root, origin_synced=False, target_synced=False)
    sid = _note(tmp_path)
    c = sqlite3.connect(origin)
    c.execute("UPDATE note_versions SET persona_id = 'p1', session_id = 's1' "
              "WHERE source_id = ?", (sid,))
    c.commit()
    c.close()
    ops.move_source(sid, "personal", "autonomy")
    GraphDB.close_all_pooled()
    t = sqlite3.connect(target)
    rows = t.execute("SELECT persona_id, session_id FROM note_versions "
                     "WHERE source_id = ?", (sid,)).fetchall()
    t.close()
    assert rows and all(r == ("p1", "s1") for r in rows)
