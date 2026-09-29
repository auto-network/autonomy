"""The org ledger shares data/orgs/<org>.db with fleet sync's back-to-back
IMMEDIATE batches. Background work waits them out (LEDGER_BUSY_TIMEOUT_S);
the default stays SQLite's short wait so a caller on the dashboard's event
loop never blocks it for long (SJC-2, 2026-09-29, and review of cfe444bc)."""

import inspect
import sqlite3
import threading
import time

from tools.network.ledger import store as ledger_store


def _held_for(path, seconds):
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")          # the sync writer holds the lock
    threading.Timer(seconds, lambda: other.execute("COMMIT")).start()
    return other


def test_a_background_open_waits_out_another_writer(tmp_path):
    path = tmp_path / "autonomy.db"
    ledger_store.LedgerStore(path).db.close()
    other = _held_for(path, 0.5)
    started = time.monotonic()
    ledger = ledger_store.LedgerStore(path, timeout=ledger_store.LEDGER_BUSY_TIMEOUT_S)
    assert time.monotonic() - started >= 0.4
    ledger.db.close()
    other.close()


def test_the_default_wait_stays_short_for_event_loop_callers():
    assert ledger_store.LEDGER_DEFAULT_TIMEOUT_S == 5.0
    assert ledger_store.LEDGER_BUSY_TIMEOUT_S >= 30.0
    ledger = ledger_store.LedgerStore()
    assert ledger.db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_the_organization_delegate_waits_long_and_off_the_event_loop():
    from tools.dashboard import org_storage_delegate, unlock_routes

    assert "timeout=LEDGER_BUSY_TIMEOUT_S" in inspect.getsource(org_storage_delegate.accept)
    assert "await asyncio.to_thread(accept, item)" in inspect.getsource(
        unlock_routes.post_unlock_vault_keys)
