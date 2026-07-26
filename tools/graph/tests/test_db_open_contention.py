"""GraphDB startup/open behavior under first-use and lock contention."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools.graph import db as db_module
from tools.graph.db import GraphDB, GraphDBNotReady


def _open_schema_state(path, barrier):
    barrier.wait()
    db = GraphDB(path)
    try:
        settings_exists = db.conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        return (
            db.read_only,
            db.conn.execute("PRAGMA user_version").fetchone()[0],
            settings_exists is not None,
        )
    finally:
        db.close()


def test_concurrent_first_open_yields_two_usable_rw_connections(tmp_path):
    path = tmp_path / "personal.db"
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_open_schema_state, path, barrier)
            for _ in range(2)
        ]
        states = [future.result(timeout=10) for future in futures]

    assert states == [
        (False, db_module._SCHEMA_USER_VERSION, True),
        (False, db_module._SCHEMA_USER_VERSION, True),
    ]


def test_rw_open_retries_until_short_write_lock_releases(
        tmp_path, monkeypatch):
    path = tmp_path / "personal.db"
    GraphDB(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 0")

    holder = sqlite3.connect(path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.15, holder.rollback)
    release.start()
    monkeypatch.setattr(db_module, "_SQLITE_CONNECT_TIMEOUT_S", 0.01)

    try:
        db = GraphDB(path)
        try:
            assert db.read_only is False
            assert db.conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0] == db_module._SCHEMA_USER_VERSION
            assert db.conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'settings'"
            ).fetchone() is not None
        finally:
            db.close()
    finally:
        release.join(timeout=1)
        if holder.in_transaction:
            holder.rollback()
        holder.close()


def test_writable_lock_exhaustion_does_not_silently_fallback(
        tmp_path, monkeypatch):
    path = tmp_path / "personal.db"
    GraphDB(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 0")

    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(db_module, "_SQLITE_CONNECT_TIMEOUT_S", 0.0)
    monkeypatch.setattr(db_module, "_RW_OPEN_BACKOFF_S", ())

    def forbidden_fallback(_self):
        raise AssertionError("writable lock must not degrade to read-only")

    monkeypatch.setattr(GraphDB, "_open_ro", forbidden_fallback)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            GraphDB(path)
    finally:
        holder.rollback()
        holder.close()


def test_schema_less_read_only_fallback_raises_not_ready(
        tmp_path, monkeypatch):
    path = tmp_path / "personal.db"
    sqlite3.connect(path).close()

    def read_only_error(_self):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(GraphDB, "_open_rw_once", read_only_error)
    monkeypatch.setattr(db_module, "_RW_OPEN_BACKOFF_S", ())

    with pytest.raises(GraphDBNotReady, match="schema is not initialized"):
        GraphDB(path)


def test_initialized_read_only_fallback_still_serves_reads(
        tmp_path, monkeypatch):
    path = tmp_path / "personal.db"
    GraphDB(path).close()

    def read_only_error(_self):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(GraphDB, "_open_rw_once", read_only_error)
    monkeypatch.setattr(db_module, "_RW_OPEN_BACKOFF_S", ())

    db = GraphDB(path)
    try:
        assert db.read_only is True
        assert db.conn.execute(
            "SELECT COUNT(*) FROM settings"
        ).fetchone()[0] >= 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.conn.execute(
                "INSERT INTO settings "
                "(id, set_id, schema_revision, key, payload) "
                "VALUES ('x', 'x', 1, 'x', '{}')"
            )
    finally:
        db.close()
