"""A store file that exists without the graph schema must not break readers.

Fleet enrollment writes its join-state table into the machine store before any
GraphDB open; before auto-nkxko the read-write inventory listing initialised
such files as a side effect. Now: read-only readers tolerate them (peer opener
→ None, settings reads fall back to the initialising open) and the activation
warm-up initialises them explicitly.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph import cross_org, org_ops, settings_ops
from tools.graph.db import GraphDB, GraphDBNotReady


@pytest.fixture
def stray_machine_store(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    GraphDB.close_all_pooled()
    machine = tmp_path / "machine.db"            # local stores live beside orgs/
    conn = sqlite3.connect(machine)
    conn.execute("create table fleet_enrollment_join_state(k text, v text)")
    conn.commit()
    conn.close()
    yield tmp_path, orgs, machine
    GraphDB.close_all_pooled()


def test_read_only_open_of_the_stray_store_is_not_ready(stray_machine_store):
    _, _, machine = stray_machine_store
    with pytest.raises(GraphDBNotReady):
        GraphDB(machine, mode="ro")


def test_peer_opener_returns_none_instead_of_raising(stray_machine_store):
    assert cross_org.open_peer_db("machine") is None


def test_settings_read_open_initialises_and_reads(stray_machine_store):
    _, _, machine = stray_machine_store
    db = settings_ops._open_read("machine")
    try:
        assert db.conn.execute(
            "select 1 from sqlite_master where type='table' and name='settings'"
        ).fetchone() is not None
        # The other component's table survived the initialisation.
        assert db.conn.execute(
            "select 1 from sqlite_master where name='fleet_enrollment_join_state'"
        ).fetchone() is not None
    finally:
        db.close()
    GraphDB(machine, mode="ro").close()           # readable now


def test_warm_up_initialises_row_less_local_stores(stray_machine_store):
    tmp_path, orgs, machine = stray_machine_store
    GraphDB.create_org_db("acme", root=orgs).close()
    GraphDB.close_all_pooled()
    assert all(o.slug != "machine" for o in org_ops.list_orgs(root=orgs))   # no row → not listed
    result = org_ops.warm_org_stores(root=orgs)
    assert result["errors"] == {}
    assert "machine" in result["warmed"] and "acme" in result["warmed"]
    GraphDB(machine, mode="ro").close()           # schema is there now
