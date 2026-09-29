"""Every SQLite connection a process opens runs synchronous=NORMAL, never FULL
(operator ruling 2026-09-29; auto-fkqz6's throughput finding: one fsync per
replicated transaction, 12 ms each, capped the apply at 83/s)."""

from __future__ import annotations

import sqlite3

from tools.graph import sqlite_defaults, sqlite_open_diag
from tools.graph.db import GraphDB

NORMAL = 1
FULL = 2


def _sync(conn) -> int:
    return int(conn.execute("PRAGMA synchronous").fetchone()[0])


def test_the_library_default_is_full_which_is_why_this_exists(tmp_path):
    raw = sqlite_open_diag._original_connect or sqlite3.connect
    conn = raw(str(tmp_path / "plain.db"))
    assert _sync(conn) == FULL
    conn.close()


def test_apply_sets_normal_and_never_fails_an_open(tmp_path):
    conn = (sqlite_open_diag._original_connect or sqlite3.connect)(str(tmp_path / "a.db"))
    assert sqlite_defaults.apply(conn) is conn and _sync(conn) == NORMAL
    conn.close()
    sqlite_defaults.apply(conn)   # closed: swallowed, the open never fails on this


def test_the_graph_database_and_the_fleet_store_open_normal(tmp_path):
    from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore
    from tools.network.idkit import KeyPair

    db = GraphDB(tmp_path / "g.db")
    assert _sync(db.conn) == NORMAL
    db.activate_fleet_sync_writers(KeyPair.generate().public_hex)
    db.close()
    conn, _catalog = SQLiteFleetSyncStore(tmp_path / "g.db")._open()
    assert _sync(conn) == NORMAL
    conn.close()


def test_install_covers_every_file_backed_connection_in_the_process(tmp_path):
    sqlite_defaults.install()
    conn = sqlite3.connect(str(tmp_path / "raw.db"))
    assert _sync(conn) == NORMAL
    conn.close()
    memory = sqlite3.connect(":memory:")   # not file-backed: untouched, harmless either way
    memory.close()


def test_the_accounting_stores_keep_their_stated_full(tmp_path):
    """One row per five-minute usage batch with a durable-before-delivery
    contract: left as written, deliberately."""
    from tools.network.accounting.ledger import UsageLedger
    from tools.network.accounting.spool import UsageSpool

    ledger = UsageLedger(tmp_path / "ledger.db")
    assert _sync(ledger._db) == FULL
    spool = UsageSpool(tmp_path / "spool.db")
    assert _sync(spool._db) == FULL
