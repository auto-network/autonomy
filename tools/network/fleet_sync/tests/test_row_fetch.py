"""Targeted row re-fetch (auto-l4h2c): a quarantined row that stored no
frame is asked for by address after every pull, served by the peer from
its catalog, and applied through ordinary apply."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync.catalog import MutationCatalog, quarantine_unrealized
from tools.network.fleet_sync.codec import encode_value
from tools.network.fleet_sync.row_fetch import (
    MAX_ROWS_REQUEST, RowFetchError, RowReceiver, decode_rows_request,
    encode_rows_request, iter_row_frames,
)
from tools.network.fleet_sync.tests.test_protocol_v4 import _eventually, _prepare, _scheduler
from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore
from tools.network.idkit import KeyPair


def _insert_source(conn, identity: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) VALUES(?,?,?,?,?,?)",
        (identity, "note", identity, "{}", "2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z"),
    )


def _insert_thought(conn, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO thoughts(id,source_id,content,role,created_at) VALUES(?,?,?,?,?)",
        (identity, source_id, "body", "user", "2026-09-19T00:00:00Z"),
    )


def test_request_codec_round_trips_and_bounds() -> None:
    blobs = [encode_value(["thoughts", ["t1"]]), encode_value(["nodes", ["n1"]])]
    assert decode_rows_request(encode_rows_request("alpha", blobs)) == ("alpha", blobs)
    with pytest.raises(RowFetchError):
        encode_rows_request("alpha", [])
    with pytest.raises(RowFetchError):
        encode_rows_request("bad:scope", blobs)
    with pytest.raises(RowFetchError):
        encode_rows_request("alpha", [b"x"] * (MAX_ROWS_REQUEST + 1))
    with pytest.raises(RowFetchError):
        decode_rows_request(b'{"v":1,"op":"rows","scope":"a","addresses":["zz"]}')


def test_the_peer_rebuilds_the_row_with_its_author_and_the_receiver_reads_it_back(tmp_path: Path) -> None:
    origin = "a" * 64
    db = GraphDB(tmp_path / "peer.db")
    catalog = MutationCatalog(db.conn, origin); catalog.install()
    with catalog.transaction(1_000, "t-parent"):
        _insert_source(db.conn, "s1")
    with catalog.transaction(1_001, "t-child"):
        _insert_thought(db.conn, "th1", "s1")
    with catalog.transaction(1_002, "t-gone"):
        db.conn.execute("DELETE FROM sources WHERE id='s1'")   # tombstone; cascades th1
    wanted = [
        encode_value(["sources", ["s1"]]),
        encode_value(["thoughts", ["th1"]]),
        encode_value(["thoughts", ["never"]]),
    ]
    items = catalog.items_at_addresses(wanted)
    by_address = {(i.mutation.table, i.mutation.address): i for i in items}
    assert ("thoughts", ("never",)) not in by_address
    gone = by_address[("sources", ("s1",))]
    assert gone.mutation.tombstone and gone.transaction_id == "t-gone" and gone.origin_incarnation == origin
    receiver = RowReceiver()
    for frame in iter_row_frames(items, 6):
        receiver.feed(frame)
    assert receiver.done and len(receiver.items) == len(items)
    assert {(i.mutation.table, i.mutation.address) for i in receiver.items} == set(by_address)
    db.close()


def test_a_frameless_orphan_is_fetched_from_the_peer_and_lands_after_a_pull(tmp_path: Path) -> None:
    """B holds the parent and the child's transaction identity but never
    got the child's row (a sweep parked it by address, no frame). After B
    pulls A, the re-fetch asks A for that address and the row lands."""
    async def run() -> None:
        root, a_key, b_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        a_path, b_path = tmp_path / "a.db", tmp_path / "b.db"
        _prepare(a_path, a_key); _prepare(b_path, b_key)
        entries = [enroll(root, machine_pub=a_key.public_hex),
                   enroll(root, machine_pub=b_key.public_hex, seq=1)]
        # A authored a parent and a child.
        a_db = GraphDB(a_path)
        a_cat = MutationCatalog(a_db.conn, a_key.public_hex)
        with a_cat.transaction(1_000, "t-parent"):
            _insert_source(a_db.conn, "s1")
        with a_cat.transaction(1_001, "t-child"):
            _insert_thought(a_db.conn, "th1", "s1")
        a_db.close()
        # B: parent present and both transactions recorded (as a sweep would
        # leave them), the child row parked with no frame.
        b_db = GraphDB(b_path)
        b_cat = MutationCatalog(b_db.conn, b_key.public_hex)
        served = {}
        a_db = GraphDB(a_path); a_cat = MutationCatalog(a_db.conn, a_key.public_hex)
        position = (0, None)
        while True:
            page = a_cat.next_transactions_for_origin(a_key.public_hex, position[0], position[1], limit=50)
            if not page:
                break
            for _ref, ts, txid, items in page:
                served[txid] = items; position = (ts, txid)
        a_db.close()
        b_cat.apply_remote_batch(served["t-parent"])
        b_cat.record_transactions([(a_key.public_hex, "t-child", 1_001)])
        quarantine_unrealized(b_db.conn, [("thoughts", ("th1",), "fk_orphan")], watermark=1_001)
        assert b_cat.frameless_hold_addresses() == [encode_value(["thoughts", ["th1"]])]
        assert b_cat.origin_watermarks()[a_key.public_hex] == 1_001, "B already claims the child"
        b_db.close()

        a = _scheduler(a_key, root, entries, a_path)
        await a.start()
        b = _scheduler(b_key, root, entries, b_path,
                       peers={a_key.public_hex: [f"ws://127.0.0.1:{a.port}"]})
        await b.start()
        try:
            def landed() -> bool:
                store = SQLiteFleetSyncStore(b_path)
                if store.frameless_hold_addresses():
                    return False
                import sqlite3
                with sqlite3.connect(b_path) as conn:
                    return conn.execute("SELECT COUNT(*) FROM thoughts WHERE id='th1'").fetchone()[0] == 1
            await _eventually(landed, timeout=10)
        finally:
            await b.stop(); await a.stop()

    asyncio.run(run())
