"""A vault store opened before its database is fleet-activated must still
capture, completely, the write it makes after activation.

Live failure (compose simulation, 2026-09-26): the invite's channel-key seal
opened its vault stores on the new organization's database moments before
the serving runtime activated that database; the seal's first write then
fired the freshly installed capture triggers on a connection with no
``fleet_sync_capture_enabled`` registered and the publish died with
"no such function". ``attach_active_production_catalog`` now leaves an
``ActivationWatch`` on an inactive connection, which attaches the catalog
under the write lock before the first replicated write after activation.
"""

from __future__ import annotations

import sqlite3

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import ActivationWatch, MutationCatalog
from tools.vault.db_content_store import DbContentStore
from tools.vault.store import VaultStore
from tools.vault.tests.test_db_content_store import _seal
from tools.vault.tests.test_policy_class import _ANCHOR, pw_factor
from tools.vault import PASSWORD_POLICY, create_class, storage_object

ORIGIN = "a" * 64


def activate(path) -> None:
    """What the serving runtime and the sync scheduler do on their own
    connection: activate capture on an already-open database file."""
    db = GraphDB(path)
    try:
        assert db.activate_fleet_sync_writers(ORIGIN) is True
    finally:
        db.close()


def capture_proof(path) -> tuple[list[tuple[str, int, int]], int]:
    """(rows, last_timestamp): each captured row's transaction id, complete
    flag and timestamp, plus the store's last stamped timestamp. A row is
    replicable only when its transaction is complete and the state's
    last_timestamp has advanced to it."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT t.transaction_id, t.complete, t.timestamp_ns "
            "FROM fleet_sync_catalog c JOIN fleet_sync_transactions t "
            "ON t.id = c.transaction_ref ORDER BY t.timestamp_ns"
        ).fetchall()
        last = int(conn.execute(
            "SELECT last_timestamp FROM fleet_sync_state WHERE singleton=1"
        ).fetchone()[0])
    finally:
        conn.close()
    return [(str(t), int(c), int(ts)) for t, c, ts in rows], last


def assert_live_captures(path, *, at_least: int, distinct_transactions: int) -> None:
    rows, last = capture_proof(path)
    live = [r for r in rows if r[0].startswith("local:")]
    assert len(live) >= at_least, rows
    assert all(complete == 1 for _, complete, _ in live), rows
    assert len({t for t, _, _ in live}) >= distinct_transactions, rows
    assert max(ts for _, _, ts in live) == last, (rows, last)


def test_vault_store_write_after_activation_by_another_connection(tmp_path):
    path = tmp_path / "org.db"
    with VaultStore(path) as store:
        assert isinstance(store.db._hook(), ActivationWatch)   # inactive at open
        activate(path)                                          # another connection activates it
        store.put_password_factor("f1", "pk", "armor")
        assert isinstance(store.db._hook(), MutationCatalog)   # attached under the write lock
        store.put_password_factor("f2", "pk2", "armor")        # context cleared: a second, new transaction
    assert_live_captures(path, at_least=2, distinct_transactions=2)


def test_vault_store_write_before_activation_then_after(tmp_path):
    path = tmp_path / "org.db"
    with VaultStore(path) as store:
        store.put_password_factor("f1", "pk", "armor")   # inactive: nothing to capture yet
        assert isinstance(store.db._hook(), ActivationWatch)
        activate(path)                                   # bootstraps f1 as legacy state
        store.put_password_factor("f2", "pk2", "armor")  # captured live
        assert isinstance(store.db._hook(), MutationCatalog)
    assert_live_captures(path, at_least=1, distinct_transactions=1)


def test_a_transaction_the_watch_did_not_open_is_refused_after_activation(tmp_path):
    """A caller's BEGIN goes through the watch (taken, re-checked under the
    lock, attached). A transaction opened past the hook -- sqlite3's implicit
    BEGIN before a local-table write, or a raw connection call -- is the one
    place the watch cannot attach; a replicated write there is refused
    loudly, never written uncaptured."""
    path = tmp_path / "org.db"
    with VaultStore(path) as store:
        activate(path)
        sqlite3.Connection.execute(store.db, "BEGIN")   # past the hook
        assert store.db.in_transaction
        try:
            store.db.execute(
                "INSERT OR REPLACE INTO vault_factors(factor_id, factor_type, public_key, armor)"
                " VALUES ('f1', 'password', 'pk', 'armor')"
            )
        except sqlite3.IntegrityError as exc:
            assert "activated on this database after this connection opened" in str(exc)
        else:
            raise AssertionError("an uncapturable write inside an open transaction was accepted")
        finally:
            store.db.rollback()
    rows, _ = capture_proof(path)
    assert not [r for r in rows if r[0].startswith("local:")]


def test_content_store_write_after_activation_by_another_connection(tmp_path):
    source = tmp_path / "source.db"
    _, locator = _seal(tmp_path, source, {"K": "v"})
    reference = storage_object.parse_locator(locator)
    header, body = DbContentStore(source).get_object(
        reference["object_id"], reference["revision_id"]
    )
    path = tmp_path / "org.db"
    with DbContentStore(path) as store:
        assert isinstance(store._db._hook(), ActivationWatch)
        activate(path)
        store.put_object(header, body)
        assert isinstance(store._db._hook(), MutationCatalog)
    assert_live_captures(path, at_least=1, distinct_transactions=1)


def test_explicit_begin_writer_after_activation_is_captured_not_refused(tmp_path):
    """put_class issues its own BEGIN IMMEDIATE before writing policy_classes
    (replicated). The watch takes that BEGIN, re-checks under the lock and
    attaches first, so the class lands captured (reviewer finding W1)."""
    path = tmp_path / "org.db"
    pub, _seed = pw_factor("alpha", "pw-1")
    record = create_class(PASSWORD_POLICY, [pub], created_at="t0", recovery=_ANCHOR)
    with VaultStore(path) as store:
        assert isinstance(store.db._hook(), ActivationWatch)
        activate(path)
        store.put_class(record)
        assert isinstance(store.db._hook(), MutationCatalog)
        assert store.get_class(record.class_id).class_id == record.class_id
    assert_live_captures(path, at_least=1, distinct_transactions=1)
