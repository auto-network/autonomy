"""Ledger events ARE Settings rows (design of record graph://53b5bb04-bc0).

One home: appending an event writes exactly one Settings row, loading a store
reads its events from those rows, and a co-member's events arrive as ordinary
replicated rows into the same place. Two "machines" are two org roots
(AUTONOMY_ORGS_DIR switched between them); row arrival on machine 2 is
simulated by writing the same rows into its own settings table — exactly what
fleet-sync materialization produces.
"""
from __future__ import annotations

import sqlite3

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair
from tools.network.ledger import settings_bridge
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger.store import LedgerStore, org_ledger_db_path

SLUG = "bridgeorg"
ORG_ID = "019c0000-0000-7000-8000-000000000777"
SEED = b"\x21" * 32
NOW_MS = 1_790_000_000_000


def _use_root(monkeypatch, root):
    GraphDB.close_all_pooled()
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)


def _found(root):
    GraphDB.create_org_db(SLUG, root=root, org_id=ORG_ID).close()
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        found_org_ledger(
            store, org_id=ORG_ID, org_root=KeyPair.generate(),
            personal_root_seed=SEED, now=NOW_MS,
        )


def _rows():
    return {
        str(m.key): (m.payload or {}).get("wire")
        for m in settings_ops.read_owned_set(settings_bridge.SET_ID, org=SLUG).members
    }


@pytest.fixture
def machine1(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path / "m1")
    _found(tmp_path / "m1")
    yield tmp_path
    GraphDB.close_all_pooled()


def test_an_event_is_stored_as_exactly_one_settings_row(machine1):
    """The row is the storage, not a copy of it."""
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        events = store.events()
    rows = _rows()
    assert {e.event_id for e in events} == set(rows)
    for event in events:
        assert rows[event.event_id] == event.to_json().decode("utf-8")


def test_the_store_reloads_its_events_from_the_rows(machine1):
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        before = {e.event_id for e in store.events()}
        genesis = store.ledger.genesis_id
    # A fresh store object reads the same rows and rebuilds the same graph.
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        assert {e.event_id for e in store.events()} == before
        assert store.ledger.genesis_id == genesis
        assert set(store.ledger.heads())  # heads computed from the graph


def test_there_is_no_second_copy_to_diverge(machine1):
    """Nothing writes ledger_events any more, so nothing can drift from it."""
    with sqlite3.connect(org_ledger_db_path(SLUG)) as conn:
        legacy = conn.execute(
            "SELECT COUNT(*) FROM ledger_events"
        ).fetchone()[0]
    assert legacy == 0
    assert len(_rows()) >= 4


def test_a_co_members_events_arrive_as_ordinary_rows(tmp_path, monkeypatch, machine1):
    """Replication delivers rows into the place the store reads from, so a
    second machine needs no absorb step at all."""
    wires = _rows()
    assert wires

    _use_root(monkeypatch, tmp_path / "m2")
    GraphDB.create_org_db(SLUG, root=tmp_path / "m2", org_id=ORG_ID).close()
    # Exactly what fleet-sync materialization produces on the receiver.
    for event_id, wire in wires.items():
        settings_ops.add_setting(
            settings_bridge.SET_ID, settings_bridge.REVISION, event_id,
            {"wire": wire}, org=SLUG, state="published",
        )
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        assert {e.event_id for e in store.events()} == set(wires)
        assert store.ledger.genesis_id is not None


def test_a_legacy_store_is_carried_across_on_open(tmp_path, monkeypatch):
    """A store written before the conversion holds its events in the old
    table; opening it moves them into the rows, once and idempotently."""
    _use_root(monkeypatch, tmp_path / "m3")
    _found(tmp_path / "m3")
    path = org_ledger_db_path(SLUG)
    wires = _rows()
    assert wires

    # Rewind to the pre-conversion shape: events in the table, no rows.
    db = GraphDB(path)
    try:
        for event_id, wire in wires.items():
            db.conn.execute(
                "INSERT OR IGNORE INTO ledger_events(event_id, event_type,"
                " author_key, hlc_ts, hlc_count, wire) VALUES(?,?,?,?,?,?)",
                (event_id, "genesis", "aa" * 32, 1, 0, wire.encode("utf-8")),
            )
        db.conn.execute(
            "DELETE FROM settings WHERE set_id=?", (settings_bridge.SET_ID,)
        )
        db.conn.commit()
    finally:
        db.close()
    assert _rows() == {}

    with LedgerStore(path) as store:
        assert {e.event_id for e in store.events()} == set(wires)
    assert set(_rows()) == set(wires)
    # Idempotent: a second open carries nothing and changes nothing.
    with sqlite3.connect(path) as conn:
        assert settings_bridge.migrate_events_to_settings(conn) == 0
    assert set(_rows()) == set(wires)


def test_a_failed_write_is_raised_not_swallowed(machine1, monkeypatch):
    """Storing an event is the append; a failure must reach the caller
    rather than leaving an event that exists in memory and nowhere else."""
    from tools.network.ledger.events import make_event
    from tools.network.ledger.hlc import HLC

    def boom(*a, **k):
        raise RuntimeError("settings write refused")

    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        monkeypatch.setattr(settings_bridge, "write_event", boom)
        event = make_event(
            KeyPair.generate(),
            {"type": "role.define", "name": "reader", "scope_set": ["read"],
             "claim_requires": "self", "version": 1},
            list(store.ledger.heads()), HLC(NOW_MS + 1, 0),
        )
        with pytest.raises(RuntimeError, match="settings write refused"):
            store.append(event)
