"""Quarantine drains for rows that hold an origin's cursor (auto-l4h2c).

A row deferred as fk_orphan (its NOT NULL parent had not arrived) or as
secondary_identity_conflict (it collided with a local row on another
unique column) is re-applied from its stored frame after every successful
pull. It lands once its prerequisite is met; until then it keeps its reason
and counts its retries.
"""
from __future__ import annotations

from pathlib import Path

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog

ORIGIN_A = "a" * 64
ORIGIN_B = "b" * 64


def _pair(tmp_path: Path):
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    server = MutationCatalog(source.conn, ORIGIN_A)
    client = MutationCatalog(target.conn, ORIGIN_B)
    server.install(); client.install()
    return source, target, server, client


def _insert_source(conn, identity: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", identity, "{}", "2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z"),
    )


def _insert_thought(conn, identity: str, source_id: str, message_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO thoughts(id,source_id,content,role,created_at,message_id) VALUES(?,?,?,?,?,?)",
        (identity, source_id, "body", "user", "2026-09-19T00:00:00Z", message_id),
    )


def _served(server, origin: str = ORIGIN_A) -> list:
    out, position = [], (0, None)
    while True:
        page = server.next_transactions_for_origin(origin, position[0], position[1], limit=50)
        if not page:
            return out
        for _ref, timestamp, transaction_id, items in page:
            out.append((timestamp, transaction_id, items))
            position = (timestamp, transaction_id)


def _quarantine(conn) -> list[tuple[str, str, int]]:
    return [
        (str(r[0]), str(r[1]), int(r[2])) for r in conn.execute(
            "SELECT table_name, reason, retries FROM fleet_sync_quarantine ORDER BY table_name"
        )
    ]


def test_an_orphan_lands_once_its_parent_arrives(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        with server.transaction(1_000, "parent"):
            _insert_source(source.conn, "s1")
        with server.transaction(1_001, "child"):
            _insert_thought(source.conn, "t1", "s1")
        served = {txid: items for _ts, txid, items in _served(server)}
        # The child arrives first: its parent is absent, so it is quarantined
        # as fk_orphan with its frame, and it holds A's cursor.
        client.apply_remote_batch(served["child"])
        assert _quarantine(target.conn) == [("thoughts", "fk_orphan", 0)]
        assert client.undrained_by_origin() == {ORIGIN_A: 1}
        assert client.origin_watermarks().get(ORIGIN_A, 0) == 0
        # A drain before the parent exists keeps the row and counts the retry.
        assert client.drain_unrealized_rows() == (0, 1)
        assert _quarantine(target.conn) == [("thoughts", "fk_orphan", 1)]
        # The parent arrives; the next drain lands the child and releases the cursor.
        client.apply_remote_batch(served["parent"])
        assert client.drain_unrealized_rows() == (1, 0)
        assert _quarantine(target.conn) == []
        assert target.conn.execute("SELECT source_id FROM thoughts WHERE id='t1'").fetchone()[0] == "s1"
        assert client.undrained_by_origin() == {}
        assert client.origin_watermarks()[ORIGIN_A] == 1_001
    finally:
        source.close(); target.close()


def test_a_conflict_lands_once_the_colliding_local_row_is_gone(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        with server.transaction(1_000, "parent"):
            _insert_source(source.conn, "s1")
        with server.transaction(1_001, "remote-thought"):
            _insert_thought(source.conn, "remote-1", "s1", message_id="m-shared")
        served = {txid: items for _ts, txid, items in _served(server)}
        # The client already holds a DIFFERENT thought with the same message id.
        with client.transaction(2_000, "local"):
            _insert_source(target.conn, "s1")
            _insert_thought(target.conn, "local-1", "s1", message_id="m-shared")
        client.apply_remote_batch(served["remote-thought"])
        assert _quarantine(target.conn) == [("thoughts", "secondary_identity_conflict", 0)]
        assert client.drain_unrealized_rows() == (0, 1)
        # The local row goes away; the drain lands the remote one.
        with client.transaction(2_001, "local-delete"):
            target.conn.execute("DELETE FROM thoughts WHERE id='local-1'")
        assert client.drain_unrealized_rows() == (1, 0)
        assert _quarantine(target.conn) == []
        assert target.conn.execute("SELECT id FROM thoughts WHERE message_id='m-shared'").fetchone()[0] == "remote-1"
    finally:
        source.close(); target.close()


def test_a_stale_quarantined_row_is_cleared_not_retried_forever(tmp_path: Path) -> None:
    """A newer winner for the same address makes the parked row inert; the
    drain clears it instead of counting retries forever."""
    source, target, server, client = _pair(tmp_path)
    try:
        with server.transaction(1_000, "parent"):
            _insert_source(source.conn, "s1")
        with server.transaction(1_001, "child"):
            _insert_thought(source.conn, "t1", "s1")
        served = {txid: items for _ts, txid, items in _served(server)}
        client.apply_remote_batch(served["child"])
        assert _quarantine(target.conn) == [("thoughts", "fk_orphan", 0)]
        # The client itself writes a newer version of the same thought address.
        with client.transaction(3_000, "local-newer"):
            _insert_source(target.conn, "s1")
            _insert_thought(target.conn, "t1", "s1")
        assert client.drain_unrealized_rows() == (1, 0)
        assert _quarantine(target.conn) == []
    finally:
        source.close(); target.close()
