"""auto-nkxko: the store inventory (list_orgs / get_org) opens read-only.

Listing the inventory used to open every org store read-write — schema init,
fleet-sync catalog attach, reconcile backfill — to read one row. It now opens
read-only, never creates anything, and skips files that are not (yet) stores.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph import org_ops
from tools.graph.db import GraphDB


@pytest.fixture
def orgs(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    GraphDB.close_all_pooled()
    for slug in ("acme", "beta"):
        GraphDB.create_org_db(slug, root=root).close()
    GraphDB.close_all_pooled()
    yield root
    GraphDB.close_all_pooled()


@pytest.fixture
def opens(monkeypatch):
    calls = []
    real = org_ops._open_org_db_ro

    def spy(path):
        calls.append(str(path))
        return real(path)
    monkeypatch.setattr(org_ops, "_open_org_db_ro", spy)
    return calls


def test_inventory_lists_stores_through_the_read_only_open(orgs, opens):
    refs = org_ops.list_orgs(root=orgs)
    assert [o.slug for o in refs] == ["acme", "beta"]
    assert sorted(opens) == sorted(str(orgs / f"{s}.db") for s in ("acme", "beta"))
    assert org_ops.get_org("acme", root=orgs) == refs[0]
    assert org_ops.get_org("nope", root=orgs) is None


def test_read_only_open_cannot_write(orgs):
    ro = org_ops._open_org_db_ro(orgs / "acme.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.conn.execute("create table t(x)")
    finally:
        ro.close()


def test_listing_never_initialises_a_stray_file(orgs):
    stray = orgs / "stray.db"
    sqlite3.connect(stray).close()                 # zero-byte sqlite file, no tables
    assert all(o.slug != "stray" for o in org_ops.list_orgs(root=orgs))
    assert stray.stat().st_size == 0, "listing must not create schema in a stray file"
    assert not (orgs / "stray.db-wal").exists()
    assert org_ops.get_org("stray", root=orgs) is None


def test_store_without_bootstrap_row_is_skipped_until_it_lands(orgs):
    partial = orgs / "gamma.db"
    conn = sqlite3.connect(partial)
    conn.execute("create table orgs(id text, slug text, type text, created_at text)")
    conn.execute("create table settings(id text)")     # looks initialised enough to open
    conn.execute("pragma user_version = 1")
    conn.commit()
    conn.close()
    assert all(o.slug != "gamma" for o in org_ops.list_orgs(root=orgs))
    conn = sqlite3.connect(partial)
    conn.execute("insert into orgs values ('g', 'gamma', 'shared', '2026-09-07')")
    conn.commit()
    conn.close()
    assert any(o.slug == "gamma" for o in org_ops.list_orgs(root=orgs))


def test_warm_org_stores_opens_each_store_read_write_once(orgs, monkeypatch):
    opened = []
    real = org_ops.GraphDB

    class Spy(real):
        def __init__(self, *a, **k):
            opened.append((str(a[0]) if a else k.get("db_path"), k.get("mode", "rw")))
            super().__init__(*a, **k)
    monkeypatch.setattr(org_ops, "GraphDB", Spy)
    result = org_ops.warm_org_stores(root=orgs)
    assert sorted(result["warmed"]) == ["acme", "beta"]
    assert result["errors"] == {}
    rw = [p for p, mode in opened if mode == "rw"]
    assert sorted(rw) == sorted(str(orgs / f"{s}.db") for s in ("acme", "beta"))


def test_warm_org_stores_reports_a_bad_store_and_continues(orgs, monkeypatch):
    real = org_ops.GraphDB

    class Boom(real):
        def __init__(self, *a, **k):
            if a and str(a[0]).endswith("acme.db") and k.get("mode", "rw") == "rw":
                raise RuntimeError("locked")
            super().__init__(*a, **k)
    monkeypatch.setattr(org_ops, "GraphDB", Boom)
    result = org_ops.warm_org_stores(root=orgs)
    assert result["warmed"] == ["beta"]
    assert result["errors"] == {"acme": "RuntimeError: locked"}
