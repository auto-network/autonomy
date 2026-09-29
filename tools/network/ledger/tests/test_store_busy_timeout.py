"""A ledger write waits for fleet sync's writer on the shared org database
instead of failing at SQLite's 5 s default (SJC-2, 2026-09-29: an organization
delegate activation failed with "database is locked" during a sync catch-up)."""

import sqlite3
import threading
import time

from tools.network.ledger import store as ledger_store


def test_the_ledger_waits_out_another_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_store, "LEDGER_BUSY_TIMEOUT_S", 5.0)
    path = tmp_path / "autonomy.db"
    ledger_store.LedgerStore(path).db.close()
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")          # the sync writer holds the lock
    threading.Timer(0.5, lambda: other.execute("COMMIT")).start()
    started = time.monotonic()
    ledger = ledger_store.LedgerStore(path)   # its schema write must wait, not fail
    assert time.monotonic() - started >= 0.4
    ledger.db.close()
    other.close()


def test_the_busy_timeout_is_longer_than_sqlites_default():
    assert ledger_store.LEDGER_BUSY_TIMEOUT_S >= 30.0
