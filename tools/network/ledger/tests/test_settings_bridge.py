"""Ledger events ride Settings (auto-dqemk): publish on append, absorb on arrival.

Two "machines" are two orgs roots (AUTONOMY_ORGS_DIR switched between them);
row arrival on machine 2 is simulated by writing the same rows into its own
settings table — exactly what fleet-sync materialization produces.
"""
from __future__ import annotations

import hashlib
import time

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


def test_append_publishes_a_row_per_event(machine1):
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        events = store.events()
    rows = _rows()
    assert set(rows) == {e.event_id for e in events}
    for event_id, wire in rows.items():
        assert hashlib.sha256(wire.encode("utf-8")).hexdigest() == event_id


def test_reconcile_backfills_missed_events(machine1, monkeypatch):
    # Wipe the set to model events appended while publish was impossible.
    db = GraphDB.for_org(SLUG)
    db.conn.execute(
        "DELETE FROM settings WHERE set_id = ?", (settings_bridge.SET_ID,)
    )
    db.conn.commit()
    GraphDB.close_all_pooled()
    assert _rows() == {}
    report = settings_bridge.reconcile(SLUG)
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        event_ids = {e.event_id for e in store.events()}
    assert report["published"] == len(event_ids)
    assert set(_rows()) == event_ids


def test_rows_absorb_into_a_second_founded_store(machine1, tmp_path, monkeypatch):
    rows = _rows()
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        genesis_id = store.ledger.genesis_id
        fold1 = store.fold(now=NOW_MS + 10)

    # Machine 2: fresh root, org DB present, genesis delivered by the join
    # flow (simulated by feeding exactly the genesis wire), rows arriving
    # via sync materialization (simulated by add_setting of the same rows).
    _use_root(monkeypatch, tmp_path / "m2")
    GraphDB.create_org_db(SLUG, root=tmp_path / "m2", org_id=ORG_ID).close()
    with LedgerStore(org_ledger_db_path(SLUG)) as store2:
        store2.append_wire(rows[genesis_id].encode("utf-8"))
    for key, wire in rows.items():
        settings_bridge.publish_event(SLUG, key, wire)

    report = settings_bridge.reconcile(SLUG)
    assert report["unappendable"] == 0
    with LedgerStore(org_ledger_db_path(SLUG)) as store2:
        assert {e.event_id for e in store2.events()} == set(rows)
        fold2 = store2.fold(now=NOW_MS + 10)
    assert fold1.members.keys() == fold2.members.keys()
    assert fold1.invites == fold2.invites


def test_corrupt_row_is_unappendable_and_isolates(machine1, tmp_path, monkeypatch):
    # Corrupt the chain's LEAF (the store's single head): exactly that one
    # row fails to absorb, and every other event still converges. A corrupt
    # mid-chain row would honestly orphan its descendants too.
    rows = _rows()
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        genesis_wire = rows[store.ledger.genesis_id]
        (leaf_id,) = store.ledger.heads()

    _use_root(monkeypatch, tmp_path / "m2")
    GraphDB.create_org_db(SLUG, root=tmp_path / "m2", org_id=ORG_ID).close()
    with LedgerStore(org_ledger_db_path(SLUG)) as store2:
        store2.append_wire(genesis_wire.encode("utf-8"))
    for key, wire in rows.items():
        settings_bridge.publish_event(
            SLUG, key, '{"corrupt": true}' if key == leaf_id else wire
        )
    report = settings_bridge.reconcile(SLUG)
    assert report["unappendable"] == 1
    with LedgerStore(org_ledger_db_path(SLUG)) as store2:
        assert len(store2.events()) == len(rows) - 1
        assert leaf_id not in store2.ledger


def test_unfounded_store_ignores_rows(machine1, tmp_path, monkeypatch):
    rows = _rows()
    _use_root(monkeypatch, tmp_path / "m2")
    GraphDB.create_org_db(SLUG, root=tmp_path / "m2", org_id=ORG_ID).close()
    for key, wire in rows.items():
        settings_bridge.publish_event(SLUG, key, wire)
    report = settings_bridge.reconcile(SLUG)
    assert report == {"published": 0, "absorbed": 0, "unappendable": 0}
    with LedgerStore(org_ledger_db_path(SLUG)) as store2:
        assert store2.ledger.genesis_id is None
