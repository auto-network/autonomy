"""Tests for the process-lifetime connection pool keyed by ``(slug, mode)``.

Spec: graph://d970d946-f95 + graph://bcce359d-a1d. The pool lets the
dashboard's server-side handlers keep writer connections open across
requests; read-only connections are a distinct slot so cross-org reads
don't fight the writer.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import GraphDB


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


def _seed(slug: str) -> None:
    GraphDB.create_org_db(slug).close()


def test_for_org_returns_cached_instance(orgs_root):
    _seed("autonomy")
    first = GraphDB.for_org("autonomy")
    second = GraphDB.for_org("autonomy")
    assert first is second  # same instance
    assert ("autonomy", "rw") in GraphDB.pooled_slots()


def test_for_org_rw_and_ro_are_distinct_slots(orgs_root):
    _seed("autonomy")
    rw = GraphDB.for_org("autonomy", mode="rw")
    ro = GraphDB.for_org("autonomy", mode="ro")
    assert rw is not ro
    assert rw.read_only is False
    assert ro.read_only is True
    slots = set(GraphDB.pooled_slots())
    assert {("autonomy", "rw"), ("autonomy", "ro")} <= slots


def test_for_org_different_slugs_are_distinct(orgs_root):
    _seed("autonomy")
    _seed("anchore")
    a = GraphDB.for_org("autonomy")
    b = GraphDB.for_org("anchore")
    assert a is not b
    assert a.db_path != b.db_path


def test_close_evicts_pool_slot(orgs_root):
    _seed("autonomy")
    db = GraphDB.for_org("autonomy")
    assert ("autonomy", "rw") in GraphDB.pooled_slots()
    db.close()
    assert ("autonomy", "rw") not in GraphDB.pooled_slots()
    # A subsequent for_org call returns a fresh instance.
    db2 = GraphDB.for_org("autonomy")
    assert db2 is not db


def test_close_all_pooled_clears_pool(orgs_root):
    _seed("autonomy")
    _seed("anchore")
    GraphDB.for_org("autonomy")
    GraphDB.for_org("anchore", mode="ro")
    assert len(GraphDB.pooled_slots()) == 2
    GraphDB.close_all_pooled()
    assert GraphDB.pooled_slots() == []


def test_for_org_missing_raises(orgs_root):
    with pytest.raises(FileNotFoundError):
        GraphDB.for_org("ghost")


def test_pooled_connection_writes_visible_on_reread(orgs_root):
    """A cached writer connection's commits are visible to its own reads
    (obvious) AND to a fresh ro-mode open after commit."""
    _seed("autonomy")
    writer = GraphDB.for_org("autonomy", mode="rw")
    writer.conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload) "
        "VALUES('s1','autonomy.test',1,'k','{}')"
    )
    writer.conn.commit()

    # Fresh ro open — WAL-visible to a new connection on the same host.
    ro = GraphDB.open_org_db("autonomy", mode="ro")
    try:
        row = ro.conn.execute(
            "SELECT id FROM settings WHERE id='s1'"
        ).fetchone()
    finally:
        ro.close()
    assert row is not None


def test_close_non_pooled_instance_safe(orgs_root):
    """Closing a non-pooled instance must not touch the pool."""
    _seed("autonomy")
    GraphDB.for_org("autonomy")
    fresh = GraphDB.open_org_db("autonomy")  # not pooled
    fresh.close()
    # Pool slot still populated.
    assert ("autonomy", "rw") in GraphDB.pooled_slots()


def test_ro_connection_usable_from_different_thread(orgs_root):
    """Regression: pooled ro connection must serve queries from a different
    thread than the one that created it.

    Before auto-7oh9k hot-patch, dashboard workers running on starlette's
    threadpool hit
    ``sqlite3.ProgrammingError: SQLite objects created in a thread can
    only be used in that same thread`` because ``check_same_thread=True``
    (Python sqlite3 default) tripped whenever a cached connection was
    reused on a different worker thread.

    Fix: ro connections open with ``check_same_thread=False``. SQLite
    serialized mode + GIL + read-only + no-cursors-held-across-awaits
    make this safe for dashboard query load.
    """
    import threading

    _seed("autonomy")
    ro = GraphDB.for_org("autonomy", mode="ro")

    errors: list[BaseException] = []

    def _run():
        try:
            ro.conn.execute("SELECT id FROM settings LIMIT 1").fetchone()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=5)
    assert not errors, f"ro connection rejected cross-thread use: {errors}"


# ── a pooled read-only handle is shared by threads; its connection is not ──
#
# Live 2026-09-15: note-open fired two get_source reads on two thread-pool
# workers in the same millisecond against the one shared connection and got
# "sqlite3.InterfaceError: bad parameter or other API misuse". The Python
# cursor and statement cache are not thread-safe even with
# check_same_thread=False. Each thread now gets its own connection, opened
# once and cached for the thread's lifetime.


def _seed_sources(slug: str, n: int) -> list[str]:
    from tools.graph.models import Source

    db = GraphDB.create_org_db(slug)
    ids = []
    for i in range(n):
        source = Source(type="note", platform="local", title=f"n{i}",
                        file_path=f"note:{i}", metadata={"i": i})
        db.insert_source(source)
        ids.append(source.id)
    db.close()
    return ids


def test_pooled_ro_handle_gives_each_thread_its_own_connection(orgs_root):
    import threading

    _seed("autonomy")
    db = GraphDB.for_org("autonomy", mode="ro")
    mine = db.conn
    assert db.conn is mine                       # cached: same thread, same connection
    # All three threads are alive at once (the barrier), so neither thread
    # ids nor connection objects can be recycled between them.
    gate = threading.Barrier(3)
    seen: list = []

    def grab():
        conn = db.conn
        assert conn is db.conn and conn is not mine
        seen.append(conn)
        gate.wait()

    threads = [threading.Thread(target=grab) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(c) for c in seen}) == 3
    # Teardown closes every thread's connection, not only the opener's.
    db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        mine.execute("SELECT 1")


def test_concurrent_reads_on_a_pooled_ro_handle_do_not_race(orgs_root):
    import threading

    ids = _seed_sources("autonomy", 40)
    db = GraphDB.for_org("autonomy", mode="ro")
    errors: list[str] = []

    def hammer():
        try:
            for _ in range(60):
                for sid in ids:
                    assert db.get_source(sid) is not None
        except Exception as exc:  # noqa: BLE001 — the whole point
            errors.append(repr(exc))

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_pooled_rw_handle_refuses_another_thread(orgs_root):
    """The read-write pooled handle (the cache GC's) keeps ONE connection
    with hook and catalog state; it is single-threaded and says so rather
    than racing silently."""
    import threading

    _seed("autonomy")
    db = GraphDB.for_org("autonomy", mode="rw")
    assert db.conn is not None
    caught: list[BaseException] = []

    def use():
        try:
            db.conn.execute("SELECT 1")
        except BaseException as exc:  # noqa: BLE001
            caught.append(exc)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert len(caught) == 1 and isinstance(caught[0], sqlite3.ProgrammingError)
