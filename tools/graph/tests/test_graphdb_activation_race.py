"""A GraphDB opened before its database is fleet-activated captures the
replicated writes it makes after another connection activates it.

Before the ActivationWatch, such a handle carried the inert pre-attach stub
(``fleet_sync_capture_enabled -> 0``): its writes fired the new triggers,
read capture as disabled and committed rows that never replicated -- silent,
permanent divergence. The two production shapes are the pooled read-write
org handle (settings upserts through GraphDB.for_org) and the per-write
GraphDB that settings_bridge.write_event opens for a ledger event.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import ActivationWatch, MutationCatalog
from tools.network.ledger import settings_bridge
from tools.vault.tests.test_store_activation_race import (
    activate, assert_live_captures, capture_proof,
)


def _insert_setting(conn, key: str) -> None:
    conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload,"
        " publication_state) VALUES(?,?,?,?,?,'published')",
        (str(uuid.uuid4()), "test.activation", 1, key, json.dumps({"k": key})),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _no_pool_leak():
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


@pytest.mark.usefixtures("no_orgs_dir_env")
def test_pooled_org_handle_captures_after_activation_by_another_connection(tmp_path):
    path = tmp_path / "race-org.db"
    GraphDB(path).close()                   # the org's file exists, inactive
    db = GraphDB.for_org("race-org", root=tmp_path)
    assert db.db_path == path
    assert isinstance(db.conn._hook(), ActivationWatch)
    activate(path)                          # the scheduler's _store_for, on its own GraphDB
    _insert_setting(db.conn, "one")         # a settings upsert through the pooled handle
    assert isinstance(db.conn._hook(), MutationCatalog)
    assert db._fleet_catalog is db.conn._hook()
    _insert_setting(db.conn, "two")
    assert_live_captures(path, at_least=2, distinct_transactions=2)


def test_write_event_captures_when_activation_lands_between_open_and_insert(tmp_path, monkeypatch):
    path = tmp_path / "org.db"
    GraphDB(path).close()                   # the file exists, inactive
    original = settings_bridge._insert

    def activate_then_insert(conn, event_id, wire):
        activate(path)                      # lands after write_event's GraphDB opened
        return original(conn, event_id, wire)

    monkeypatch.setattr(settings_bridge, "_insert", activate_then_insert)
    assert settings_bridge.write_event(path, "e" * 64, "wire-bytes") is True
    assert_live_captures(path, at_least=1, distinct_transactions=1)


def test_unwatched_handle_fails_loudly_instead_of_committing_uncaptured(tmp_path):
    """attach_fleet_sync=False installs no watch; if such a handle ever writes
    a replicated row after activation, the stub refuses rather than lets
    the row commit uncaptured."""
    path = tmp_path / "org.db"
    db = GraphDB(path, attach_fleet_sync=False)
    try:
        activate(path)
        with pytest.raises(Exception) as caught:
            _insert_setting(db.conn, "one")
        assert "activated on this database after this connection opened" in str(caught.value) \
            or "user-defined function raised exception" in str(caught.value)
        db.conn.rollback()
    finally:
        db.close()
    rows, _ = capture_proof(path)
    assert not [r for r in rows if r[0].startswith("local:")]


def test_explicit_begin_immediate_on_a_pre_activation_handle_is_captured(tmp_path):
    """settings_ops opens its own BEGIN IMMEDIATE before writing settings; on a
    handle opened before activation the watch takes the BEGIN, attaches
    under the lock, and the transaction is captured and complete."""
    path = tmp_path / "org.db"
    db = GraphDB(path)
    try:
        activate(path)
        db.conn.execute("BEGIN IMMEDIATE")
        assert isinstance(db.conn._hook(), MutationCatalog)
        assert db.conn.in_transaction
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload,"
            " publication_state) VALUES(?,?,?,?,?,'published')",
            (str(uuid.uuid4()), "test.activation", 1, "one", json.dumps({"k": 1})),
        )
        db.conn.commit()
    finally:
        db.close()
    assert_live_captures(path, at_least=1, distinct_transactions=1)


def test_plain_begin_on_a_watched_connection_takes_no_write_lock(tmp_path):
    """A DEFERRED BEGIN is a consistent read (the scheduler's page reads);
    the watch leaves it alone, so a concurrent writer is not blocked."""
    path = tmp_path / "org.db"
    reader = GraphDB(path)
    writer = GraphDB(path)
    try:
        assert isinstance(reader.conn._hook(), ActivationWatch)
        reader.conn.execute("BEGIN")
        reader.conn.execute("SELECT COUNT(*) FROM settings").fetchone()
        assert reader.conn.in_transaction
        writer.conn.execute("BEGIN IMMEDIATE")        # would raise "database is locked" if the reader held the write lock
        assert writer.conn.in_transaction
        writer.conn.rollback()
        reader.conn.rollback()
    finally:
        writer.close()
        reader.close()


def test_begin_exclusive_on_a_watched_connection_is_issued_as_exclusive(tmp_path):
    """The watch re-issues the caller's own text: an EXCLUSIVE stays
    EXCLUSIVE (traced on the connection), before and after activation."""
    path = tmp_path / "org.db"
    db = GraphDB(path)
    traced: list[str] = []
    try:
        db.conn.set_trace_callback(traced.append)
        db.conn.execute("BEGIN EXCLUSIVE")
        db.conn.rollback()
        activate(path)
        db.conn.execute("BEGIN EXCLUSIVE")          # taken: re-check, attach, re-issue
        assert isinstance(db.conn._hook(), MutationCatalog)
        db.conn.rollback()
    finally:
        db.conn.set_trace_callback(None)
        db.close()
    begins = [t.strip().upper() for t in traced if t.strip().upper().startswith("BEGIN")]
    assert begins and all(b == "BEGIN EXCLUSIVE" for b in begins), begins
    assert len(begins) == 3                          # before; taken-then-rolled-back; re-issued
