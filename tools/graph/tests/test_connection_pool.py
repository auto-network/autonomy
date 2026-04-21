"""Tests for the process-lifetime connection pool keyed by ``(slug, mode)``.

Spec: graph://d970d946-f95 + graph://bcce359d-a1d. The pool lets the
dashboard's server-side handlers keep writer connections open across
requests; read-only connections are a distinct slot so cross-org reads
don't fight the writer.
"""

from __future__ import annotations

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
