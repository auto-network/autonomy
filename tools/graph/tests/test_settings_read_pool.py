"""Settings reads borrow one pooled read-only handle per database instead of
opening and closing a connection per read (auto-dvdzl, 2026-09-30)."""

from __future__ import annotations

import sqlite3
import threading

from tools.graph import settings_ops
from tools.graph.db import _pooled_read_handle
from tools.graph.db import GraphDB


def _db(tmp_path):
    path = tmp_path / "store.db"
    GraphDB(path).close()          # create a graph-schema database
    return path


def test_reads_share_one_handle_and_close_returns_it(tmp_path):
    path = _db(tmp_path)
    GraphDB.close_all_pooled()
    first = _pooled_read_handle(path)
    first.close()                  # a borrowed close keeps the pooled handle open
    second = _pooled_read_handle(path)
    assert first._db is second._db
    assert second.conn.execute("SELECT 1").fetchone()[0] == 1
    GraphDB.close_all_pooled()     # the shared pool still owns shutdown
    third = _pooled_read_handle(path)
    assert third._db is not first._db
    GraphDB.close_all_pooled()


def test_a_borrowed_attribute_never_touches_the_pooled_handle(tmp_path):
    path = _db(tmp_path)
    GraphDB.close_all_pooled()
    borrowed = _pooled_read_handle(path)
    borrowed.conn = "wrapped"
    assert _pooled_read_handle(path).conn != "wrapped"
    GraphDB.close_all_pooled()


def test_each_thread_reads_on_its_own_connection_and_sees_new_writes(tmp_path):
    path = _db(tmp_path)
    GraphDB.close_all_pooled()
    main_conn = _pooled_read_handle(path).conn
    seen = {}

    def worker():
        seen["conn"] = _pooled_read_handle(path).conn

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["conn"] is not main_conn

    writer = sqlite3.connect(path)
    with writer:
        writer.execute("CREATE TABLE probe (x)")
        writer.execute("INSERT INTO probe VALUES (7)")
    writer.close()
    assert _pooled_read_handle(path).conn.execute(
        "SELECT x FROM probe").fetchone()[0] == 7
    GraphDB.close_all_pooled()


def test_the_peer_subscription_lookup_opens_personal_db_once(tmp_path, monkeypatch):
    """resolve_peers runs on every organization read; its personal.db lookup
    borrows the pooled handle instead of opening a connection per call."""
    from tools.graph import cross_org, db as db_mod

    personal = tmp_path / "personal.db"
    GraphDB(personal).close()
    GraphDB.close_all_pooled()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setattr(cross_org, "resolve_caller_db_path", lambda slug: str(personal))
    opened = []
    real_init = db_mod.GraphDB.__init__

    def counting_init(self, *args, **kwargs):
        opened.append(args[0] if args else kwargs.get("path"))
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(db_mod.GraphDB, "__init__", counting_init)
    for _ in range(5):
        assert cross_org._read_peer_subscription("anchore") is None
    assert len(opened) == 1, opened
    GraphDB.close_all_pooled()
