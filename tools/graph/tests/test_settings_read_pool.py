"""Settings reads borrow one pooled read-only handle per database instead of
opening and closing a connection per read (auto-dvdzl, 2026-09-30)."""

from __future__ import annotations

import sqlite3
import threading

from tools.graph import settings_ops
from tools.graph.db import GraphDB


def _db(tmp_path):
    path = tmp_path / "store.db"
    GraphDB(path).close()          # create a graph-schema database
    return path


def test_reads_share_one_handle_and_close_returns_it(tmp_path):
    path = _db(tmp_path)
    GraphDB.close_all_pooled()
    first = settings_ops._pooled_read_handle(path)
    first.close()                  # a borrowed close keeps the pooled handle open
    second = settings_ops._pooled_read_handle(path)
    assert first._db is second._db
    assert second.conn.execute("SELECT 1").fetchone()[0] == 1
    GraphDB.close_all_pooled()     # the shared pool still owns shutdown
    third = settings_ops._pooled_read_handle(path)
    assert third._db is not first._db
    GraphDB.close_all_pooled()


def test_a_borrowed_attribute_never_touches_the_pooled_handle(tmp_path):
    path = _db(tmp_path)
    GraphDB.close_all_pooled()
    borrowed = settings_ops._pooled_read_handle(path)
    borrowed.conn = "wrapped"
    assert settings_ops._pooled_read_handle(path).conn != "wrapped"
    GraphDB.close_all_pooled()


def test_each_thread_reads_on_its_own_connection_and_sees_new_writes(tmp_path):
    path = _db(tmp_path)
    GraphDB.close_all_pooled()
    main_conn = settings_ops._pooled_read_handle(path).conn
    seen = {}

    def worker():
        seen["conn"] = settings_ops._pooled_read_handle(path).conn

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["conn"] is not main_conn

    writer = sqlite3.connect(path)
    with writer:
        writer.execute("CREATE TABLE probe (x)")
        writer.execute("INSERT INTO probe VALUES (7)")
    writer.close()
    assert settings_ops._pooled_read_handle(path).conn.execute(
        "SELECT x FROM probe").fetchone()[0] == 7
    GraphDB.close_all_pooled()
