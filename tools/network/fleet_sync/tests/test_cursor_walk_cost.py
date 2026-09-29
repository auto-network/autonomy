"""The cursor walk and the heads pages cost the same wherever the cursor
sits in an origin (auto-fkqz6 part 5).

SJC-2, 2026-09-29 17:47Z (thread dump): the apply thread sat in
_advance_cursor, one query per transaction, whose predicate
``timestamp_ns>? OR (timestamp_ns=? AND transaction_id>?)`` SQLite served by
scanning the origin's index from its first row on every step: 22.8 ms per
step at row 300k of 400k here, ~40 min per 100k steps, inside the apply's
write lock. A row-value comparison on the index columns is served by one
index seek.
"""

from __future__ import annotations

import sqlite3
import time

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import (
    MutationCatalog,
    ensure_origin_cursor_schema,
)
from tools.network.idkit import KeyPair

BASE = 1_790_000_000_000_000_000


def _store_with_origin(tmp_path, rows: int):
    key = KeyPair.generate()
    db = GraphDB(tmp_path / "walk.db")
    db.activate_fleet_sync_writers(key.public_hex)
    conn = db.conn
    catalog = MutationCatalog(conn, key.public_hex)
    ensure_origin_cursor_schema(conn)
    conn.execute("INSERT OR IGNORE INTO fleet_sync_origins(incarnation) VALUES(?)", ("peer" * 16,))
    origin_id = int(conn.execute("SELECT id FROM fleet_sync_origins WHERE incarnation=?", ("peer" * 16,)).fetchone()[0])
    conn.executemany(
        "INSERT INTO fleet_sync_transactions(origin_id,transaction_id,timestamp_ns,complete) VALUES(?,?,?,1)",
        ((origin_id, f"local:{i:032x}", BASE + i * 1000) for i in range(rows)),
    )
    conn.commit()
    return db, catalog, origin_id


def test_the_cursor_walk_from_deep_in_an_origin_is_flat(tmp_path):
    rows = 120_000
    db, catalog, origin_id = _store_with_origin(tmp_path, rows)
    try:
        start = 100_000
        db.conn.execute(
            "INSERT INTO fleet_sync_origin_cursor(origin_id,timestamp_ns,transaction_id) VALUES(?,?,?)",
            (origin_id, BASE + start * 1000, f"local:{start:032x}"),
        )
        db.conn.commit()   # the walk opens its own write transaction, as the apply does
        db.conn.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        catalog._advance_cursor(origin_id)
        took = time.monotonic() - started
        db.conn.execute("COMMIT")
        ts, txid = db.conn.execute(
            "SELECT timestamp_ns, transaction_id FROM fleet_sync_origin_cursor WHERE origin_id=?", (origin_id,)
        ).fetchone()
        assert (ts, txid) == (BASE + (rows - 1) * 1000, f"local:{rows - 1:032x}")
        # 20,000 steps from row 100k: the old predicate needed ~8 ms each here.
        assert took < 5.0, f"walk of {rows - start} steps took {took:.1f}s"
    finally:
        db.close()


def test_heads_pages_deep_in_an_origin_are_one_index_seek(tmp_path):
    rows = 120_000
    db, catalog, origin_id = _store_with_origin(tmp_path, rows)
    try:
        start = 100_000
        started = time.monotonic()
        position: tuple = (BASE + start * 1000, f"local:{start:032x}")
        served = 0
        while True:
            page = catalog.next_transaction_heads_for_origin("peer" * 16, position[0], position[1], limit=200)
            if not page:
                break
            served += len(page)
            position = (page[-1][1], page[-1][2])
        took = time.monotonic() - started
        assert served == rows - start - 1
        assert took < 5.0, f"{served // 200} pages took {took:.1f}s"
        # The plan seeks the index on (origin, timestamp, id); it does not scan from the origin's first row.
        plan = " ".join(str(r[3]) for r in db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT t.id FROM fleet_sync_transactions t JOIN fleet_sync_origins o ON o.id=t.origin_id "
            "WHERE o.incarnation=? AND (t.timestamp_ns, t.transaction_id) > (?, ?) ORDER BY t.timestamp_ns, t.transaction_id LIMIT 200",
            ("peer" * 16, BASE, "x")))
        assert "(timestamp_ns,transaction_id)>(?,?)" in plan, plan
    finally:
        db.close()
