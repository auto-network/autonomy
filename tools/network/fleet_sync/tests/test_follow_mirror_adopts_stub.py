"""First-run follow seeding meets a stub at the mirror's path: a database a
path resolver minted before the seeding ran, with no orgs row. It used to be
left untouched forever, so the scheduler treated it as a member organization
and the follow path never ran (Windows signed-release node, 2026-09-26:
is_followed_org('autonomy') False, zero follow lines). The stub is adopted
as the followed mirror instead."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.graph import db as graph_db
from tools.graph.db import GraphDB
from tools.network import fleet_sync_scheduler as fss

ORG = "2d4b90cb-1e89-452b-82cb-68ca44fd8e52"
ROW = {"org_uuid": ORG, "rendezvous": "https://relay.test/l/abc", "link_pub": "ff" * 32, "enabled": True}


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    personal = tmp_path / "personal.db"
    (tmp_path / "orgs").mkdir()
    monkeypatch.setattr(graph_db, "_org_db_path", lambda slug, root=None: personal if slug == "personal" else tmp_path / "orgs" / f"{slug}.db")
    monkeypatch.setattr(fss, "_enabled_follow_rows", lambda: [("autonomy", dict(ROW))])
    graph_db._ORG_TYPE_CACHE.clear()
    return tmp_path


def _org_row(path: Path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, type FROM orgs LIMIT 1").fetchone()
    finally:
        conn.close()


def test_an_untyped_stub_at_the_mirror_path_is_adopted_as_the_followed_mirror(data_root):
    stub = data_root / "orgs" / "autonomy.db"
    GraphDB(stub).close()                            # minted by a path resolver: schema, no orgs row
    assert _org_row(stub) is None
    assert fss.materialize_follow_scopes() == ["autonomy"]
    assert _org_row(stub) == (ORG, "followed")
    assert fss.materialize_follow_scopes() == []     # idempotent: now a real mirror


def test_a_file_that_belongs_to_another_org_is_still_refused(data_root):
    other = data_root / "orgs" / "autonomy.db"
    GraphDB.create_org_db("autonomy", type_="shared", org_id="11111111-1111-4111-8111-111111111111", path=other).close()
    assert fss.materialize_follow_scopes() == []
    assert _org_row(other) == ("11111111-1111-4111-8111-111111111111", "shared")


def test_adopt_org_db_refuses_a_non_stub(tmp_path):
    path = tmp_path / "x.db"
    GraphDB.create_org_db("x", type_="shared", org_id="22222222-2222-4222-8222-222222222222", path=path).close()
    with pytest.raises(FileExistsError, match="not a stub"):
        GraphDB.adopt_org_db("x", type_="followed", org_id=ORG, path=path)
    with pytest.raises(FileNotFoundError):
        GraphDB.adopt_org_db("y", type_="followed", org_id=ORG, path=tmp_path / "missing.db")


def test_a_file_with_content_but_no_org_row_is_refused_and_left_untouched(data_root):
    """Content without an orgs row is somebody's data, not a stub: adopting
    it as a read-only mirror would land remote rows on local ones."""
    path = data_root / "orgs" / "autonomy.db"
    db = GraphDB(path)
    db.conn.execute(
        "INSERT INTO sources(id, type, title, metadata, created_at, ingested_at) "
        "VALUES('s1','note','local','{}','2026-09-26T00:00:00Z','2026-09-26T00:00:00Z')"
    )
    db.conn.commit(); db.close()
    with pytest.raises(FileExistsError, match="sources=1"):
        GraphDB.adopt_org_db("autonomy", type_="followed", org_id=ORG, path=path)
    assert fss.materialize_follow_scopes() == []          # refused, logged, left alone
    assert _org_row(path) is None
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_follow_round_materializes_the_mirror_itself(data_root, monkeypatch):
    """A standalone node has no connector to call materialize_follow_scopes
    at start-up; the round does it, so a stub minted after start-up is
    adopted before the pull is attempted."""
    import asyncio
    from tools.network.idkit import KeyPair

    stub = data_root / "orgs" / "autonomy.db"
    GraphDB(stub).close()
    monkeypatch.setattr(graph_db, "is_followed_org",
                        lambda slug, root=None: (_org_row(data_root / "orgs" / f"{slug}.db") or (None, None))[1] == "followed")
    attempted = []

    async def connect(row):
        attempted.append(row["org_uuid"])
        raise RuntimeError("no relay in this test")

    scheduler = fss.FleetSyncScheduler(fss.FleetSyncRuntimeConfig(
        machine_key=KeyPair.generate(), personal_root_pub=KeyPair.generate().public_hex,
        roster_entries=lambda: (), peer_addresses=lambda: {},
        personal_db_path=data_root / "personal.db", poll_interval=60.0,
        sync_scopes=lambda: {"autonomy": stub}, follow_connect=connect,
    ))
    asyncio.run(scheduler._sync_follow_scopes(0.0))
    assert _org_row(stub) == (ORG, "followed")        # adopted by the round
    assert attempted == [ORG]                          # and the pull was attempted as a follow
