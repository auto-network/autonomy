"""A transaction whose row count is an exact multiple of the serve group
size completes on the puller (auto-fkqz6, 2026-09-29).

``transaction_group`` judged ``more`` by "the slice came back full", so a
transaction of exactly SERVE_GROUP_OPERATIONS rows (or any multiple) served
its last group with last=false and then nothing: the puller held every
operation and never the completion, its cursor for that origin stuck below
the transaction forever. SJC-2 held 41 such transactions from Home, each
exactly 2,000 rows, all complete=0, the autonomy cursor pinned at
2026-09-27 22:27Z while Home held the same rows as complete=1.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network import fleet_sync_scheduler as scheduler_mod
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync_scheduler import (
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    SQLiteFleetSyncStore,
)
from tools.network.idkit import KeyPair


def _prepare(path: Path, machine: KeyPair) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(machine.public_hex)
    finally:
        db.close()


def _write_one_transaction(path: Path, rows: int, tag: str) -> None:
    """ONE SQLite transaction = ONE fleet transaction of *rows* operations."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = GraphDB(path)
    try:
        with db.conn:
            db.conn.executemany(
                "INSERT INTO sources(id, type, title, metadata, created_at, ingested_at) VALUES(?,?,?,?,?,?)",
                [(f"{tag}-{j:05d}", "note", f"{tag} {j}", "{}", now, now) for j in range(rows)],
            )
    finally:
        db.close()


def _transactions(path: Path) -> list[tuple[str, int, int]]:
    with sqlite3.connect(path) as conn:
        return [
            (str(r[0]), int(r[1]), int(r[2])) for r in conn.execute(
                "SELECT t.transaction_id, t.complete, COUNT(c.transaction_ref) FROM fleet_sync_transactions t "
                "LEFT JOIN fleet_sync_catalog c ON c.transaction_ref=t.id GROUP BY t.id ORDER BY t.timestamp_ns"
            )
        ]


def test_the_last_group_of_an_exact_multiple_transaction_says_so(tmp_path):
    key = KeyPair.generate()
    path = tmp_path / "s.db"
    _prepare(path, key)
    _write_one_transaction(path, 6, "six")
    conn, catalog = SQLiteFleetSyncStore(path)._open()
    try:
        (ref, _ts, txid) = catalog.next_transaction_heads_for_origin(catalog.origin_incarnation, 0, None, limit=10)[0]
        # limit 6 over 6 rows: one group, and it is the last.
        items, more = catalog.transaction_group(ref, catalog.origin_incarnation, txid, offset=0, limit=6)
        assert len(items) == 6 and more is False
        # limit 3 over 6 rows: two full groups; the second is the last.
        items, more = catalog.transaction_group(ref, catalog.origin_incarnation, txid, offset=0, limit=3)
        assert len(items) == 3 and more is True
        items, more = catalog.transaction_group(ref, catalog.origin_incarnation, txid, offset=3, limit=3)
        assert len(items) == 3 and more is False
        # limit 4 over 6 rows: a full group, then a short last one.
        items, more = catalog.transaction_group(ref, catalog.origin_incarnation, txid, offset=0, limit=4)
        assert len(items) == 4 and more is True
        items, more = catalog.transaction_group(ref, catalog.origin_incarnation, txid, offset=4, limit=4)
        assert len(items) == 2 and more is False
    finally:
        conn.close()


def test_a_transaction_of_exactly_one_group_completes_on_the_puller_and_its_cursor_passes_it(tmp_path, monkeypatch):
    """End to end over a real direct listener: the server holds transactions
    of exactly one group, exactly two groups, and one short row; after one
    pull the puller holds every one as complete and its cursor for the
    server's origin is at the server's newest transaction."""
    monkeypatch.setattr(scheduler_mod, "SERVE_GROUP_OPERATIONS", 50)
    root, server_key, puller_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    server_db, puller_db = tmp_path / "server.db", tmp_path / "puller.db"
    _prepare(server_db, server_key)
    _prepare(puller_db, puller_key)
    # A seed row first: the puller's first round is a bootstrap SWEEP, which
    # pages by address, not by transaction group. The defect is in the DELTA
    # serve (SJC-2 was pulling deltas), so the exact-multiple transactions
    # are written after the sweep has completed.
    _write_one_transaction(server_db, 1, "seed")
    entries = [enroll(root, machine_pub=server_key.public_hex), enroll(root, machine_pub=puller_key.public_hex)]

    async def run():
        server = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=server_key, personal_root_pub=root.public_hex, roster_entries=lambda: entries,
            peer_addresses=lambda: {}, personal_db_path=server_db, poll_interval=0.05,
            min_backoff=0.01, max_backoff=0.05, listen_host="127.0.0.1", listen_port=0))
        await server.start()
        puller = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=puller_key, personal_root_pub=root.public_hex, roster_entries=lambda: entries,
            peer_addresses=lambda: {server_key.public_hex: [f"ws://127.0.0.1:{server.port}"]},
            personal_db_path=puller_db, poll_interval=0.05, min_backoff=0.01, max_backoff=0.05))
        await puller.start()
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not any(t.startswith("local:") for t, c, _n in _transactions(puller_db) if c):
                await asyncio.sleep(0.2)
            assert [c for _t, c, _n in _transactions(puller_db)] == [1], "the sweep round did not complete"
            _write_one_transaction(server_db, 50, "one-group")      # exactly SERVE_GROUP_OPERATIONS
            _write_one_transaction(server_db, 100, "two-groups")    # exactly 2x
            _write_one_transaction(server_db, 1, "short")
            expected = _transactions(server_db)
            assert [c for _t, c, _n in expected] == [1, 1, 1, 1] and [n for _t, _c, n in expected] == [1, 50, 100, 1]
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                held = {t: (c, n) for t, c, n in _transactions(puller_db)}
                if all(held.get(t) == (1, n) for t, _c, n in expected):
                    break
                await asyncio.sleep(0.2)
            held = {t: (c, n) for t, c, n in _transactions(puller_db)}
            for t, _c, n in expected:
                assert held.get(t) == (1, n), (t, held.get(t))
            # The cursor for the server's origin stands at its newest transaction.
            conn, catalog = SQLiteFleetSyncStore(puller_db)._open()
            try:
                positions = catalog.origin_watermarks()
                unresolved = catalog.unresolved_transactions()
            finally:
                conn.close()
            with sqlite3.connect(server_db) as sconn:
                newest = int(sconn.execute("SELECT MAX(timestamp_ns) FROM fleet_sync_transactions").fetchone()[0])
                origin = str(sconn.execute("SELECT origin_incarnation FROM fleet_sync_state WHERE singleton=1").fetchone()[0])
            assert positions[origin] >= newest and origin not in unresolved
        finally:
            await puller.stop()
            await server.stop()
    asyncio.run(run())


def test_a_serve_stream_opens_the_store_once_for_all_its_pages_and_groups(tmp_path, monkeypatch):
    """Every heads page and every transaction group used to open a fresh
    connection and re-attach the catalog (~2 s each on Home's store,
    auto-fkqz6). One serve stream now opens the store exactly once on its
    own thread, however many transactions and groups it serves."""
    monkeypatch.setattr(scheduler_mod, "SERVE_GROUP_OPERATIONS", 5)
    monkeypatch.setattr(scheduler_mod, "SERVE_PAGE_TRANSACTIONS", 4)
    sessions: list = []
    real_init = scheduler_mod._ServeSession.__init__

    def recording_init(self, store):
        real_init(self, store)
        sessions.append(self)

    monkeypatch.setattr(scheduler_mod._ServeSession, "__init__", recording_init)
    root, server_key, puller_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    server_db, puller_db = tmp_path / "server.db", tmp_path / "puller.db"
    _prepare(server_db, server_key)
    _prepare(puller_db, puller_key)
    _write_one_transaction(server_db, 1, "seed")
    entries = [enroll(root, machine_pub=server_key.public_hex), enroll(root, machine_pub=puller_key.public_hex)]

    async def run():
        server = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=server_key, personal_root_pub=root.public_hex, roster_entries=lambda: entries,
            peer_addresses=lambda: {}, personal_db_path=server_db, poll_interval=0.05,
            min_backoff=0.01, max_backoff=0.05, listen_host="127.0.0.1", listen_port=0))
        await server.start()
        puller = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=puller_key, personal_root_pub=root.public_hex, roster_entries=lambda: entries,
            peer_addresses=lambda: {server_key.public_hex: [f"ws://127.0.0.1:{server.port}"]},
            personal_db_path=puller_db, poll_interval=0.05, min_backoff=0.01, max_backoff=0.05))
        await puller.start()
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not any(c for _t, c, _n in _transactions(puller_db)):
                await asyncio.sleep(0.2)
            # 30 transactions of 12 rows: 8 heads pages, 3 groups each.
            for i in range(30):
                _write_one_transaction(server_db, 12, f"tx{i:02d}")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and sum(1 for _t, c, _n in _transactions(puller_db) if c) < 31:
                await asyncio.sleep(0.2)
            assert sum(1 for _t, c, _n in _transactions(puller_db) if c) == 31
        finally:
            await puller.stop()
            await server.stop()
        assert sessions, "no serve stream ran"
        assert all(session.opens <= 1 for session in sessions), [s.opens for s in sessions]
        assert any(session.opens == 1 for session in sessions)
        assert all(session._conn is None for session in sessions), "a serve left its connection open"
    asyncio.run(run())


def test_a_transaction_already_held_incomplete_flips_complete_when_reserved(tmp_path, monkeypatch):
    """The recovery path: a puller that received the last group of an
    exact-multiple transaction under the old rule (last=false, nothing
    after) holds every row and complete=0. When the fixed server re-serves
    it, the group arrives as pure duplicates with last=true and the
    transaction must flip to complete so the cursor can pass it (SJC-2's
    local:7bc8d6be…, 2,000 rows on both sides, complete=1 on Home and 0 on
    SJC-2 after the fixed serve, 2026-09-29 17:43Z)."""
    from tools.network.fleet_sync import catalog as catalog_mod

    monkeypatch.setattr(scheduler_mod, "SERVE_GROUP_OPERATIONS", 50)
    root, server_key, puller_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    server_db, puller_db = tmp_path / "server.db", tmp_path / "puller.db"
    _prepare(server_db, server_key)
    _prepare(puller_db, puller_key)
    _write_one_transaction(server_db, 1, "seed")
    entries = [enroll(root, machine_pub=server_key.public_hex), enroll(root, machine_pub=puller_key.public_hex)]
    fixed = catalog_mod.MutationCatalog.transaction_group

    def old_rule(self, transaction_ref, incarnation, transaction_id, *, offset, limit, projection=catalog_mod.Projection.FULL):
        items, _more = fixed(self, transaction_ref, incarnation, transaction_id, offset=offset, limit=limit, projection=projection)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM fleet_sync_catalog WHERE transaction_ref=? ORDER BY operation_index LIMIT ? OFFSET ?)",
            (int(transaction_ref), int(limit), int(offset))).fetchone()[0]
        return items, n == int(limit)

    def _row(path, prefix):
        return [(c, n) for t, c, n in _transactions(path) if t.startswith("local:") and n == 50]

    async def run():
        server = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=server_key, personal_root_pub=root.public_hex, roster_entries=lambda: entries,
            peer_addresses=lambda: {}, personal_db_path=server_db, poll_interval=0.05,
            min_backoff=0.01, max_backoff=0.05, listen_host="127.0.0.1", listen_port=0))
        await server.start()
        puller = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=puller_key, personal_root_pub=root.public_hex, roster_entries=lambda: entries,
            peer_addresses=lambda: {server_key.public_hex: [f"ws://127.0.0.1:{server.port}"]},
            personal_db_path=puller_db, poll_interval=0.05, min_backoff=0.01, max_backoff=0.05))
        await puller.start()
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not any(c for _t, c, _n in _transactions(puller_db)):
                await asyncio.sleep(0.2)
            # Phase 1: the old rule serves a transaction of exactly one group.
            monkeypatch.setattr(catalog_mod.MutationCatalog, "transaction_group", old_rule)
            _write_one_transaction(server_db, 50, "exact")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and _row(puller_db, "exact") != [(0, 50)]:
                await asyncio.sleep(0.2)
            assert _row(puller_db, "exact") == [(0, 50)], "the old rule should leave it held incomplete"
            # Phase 2: the fixed server re-serves it from the puller's cursor.
            monkeypatch.setattr(catalog_mod.MutationCatalog, "transaction_group", fixed)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and _row(puller_db, "exact") != [(1, 50)]:
                await asyncio.sleep(0.2)
            assert _row(puller_db, "exact") == [(1, 50)], _row(puller_db, "exact")
        finally:
            await puller.stop()
            await server.stop()
    asyncio.run(run())
