"""The regression the previous shape could not see: an append against a REAL,
fleet-activated organization database, where the settings capture triggers are
present and call functions a bare connection does not have.

Every other test for this path uses a synthetic store, in which both the
trigger and the function are absent, so the failure was invisible by
construction (live 500 on every invite mint, 2026-09-08).
"""
from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger.store import LedgerStore, org_ledger_db_path

SLUG = "capturedorg"
ORG_ID = "019c0000-0000-7000-8000-0000000009aa"


@pytest.fixture
def activated(tmp_path, monkeypatch):
    GraphDB.close_all_pooled()
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db(SLUG, root=root, org_id=ORG_ID).close()
    db = GraphDB(org_ledger_db_path(SLUG))
    try:
        db.activate_fleet_sync_writers("cc" * 32)
    finally:
        db.close()
    yield org_ledger_db_path(SLUG)
    GraphDB.close_all_pooled()


def _capture_trigger_present(path) -> bool:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='settings' LIMIT 1"
        ).fetchone() is not None


def test_founding_appends_against_a_fleet_activated_store(activated):
    assert _capture_trigger_present(activated), "fixture must have capture triggers"
    with LedgerStore(activated) as store:
        found_org_ledger(
            store, org_id=ORG_ID, org_root=KeyPair.generate(),
            personal_root_seed=b"\x33" * 32, now=1_790_000_000_000,
        )
        assert len(store.events()) >= 4
        assert store.ledger.genesis_id


def test_each_event_is_captured_for_replication(activated):
    """Being stored IS being on the wire: the same INSERT that persists the
    event is the one the capture triggers journal."""
    with LedgerStore(activated) as store:
        found_org_ledger(
            store, org_id=ORG_ID, org_root=KeyPair.generate(),
            personal_root_seed=b"\x44" * 32, now=1_790_000_000_000,
        )
        event_ids = {e.event_id for e in store.events()}
    # The catalog address is a length-prefixed BLOB, not text, so it is
    # matched as bytes rather than with a LIKE pattern.
    with sqlite3.connect(activated) as conn:
        captured = [
            row[0] for row in conn.execute(
                "SELECT address FROM fleet_sync_catalog "
                "WHERE instr(address, CAST(? AS BLOB)) > 0",
                (b"autonomy.org.ledger-event",),
            )
        ]
    assert len(captured) >= len(event_ids), (len(captured), len(event_ids))
    for event_id in event_ids:
        assert any(event_id.encode() in bytes(a) for a in captured), event_id


def test_a_later_append_also_captures(activated):
    from tools.network.ledger.events import make_event
    from tools.network.ledger.hlc import HLC

    with LedgerStore(activated) as store:
        found_org_ledger(
            store, org_id=ORG_ID, org_root=KeyPair.generate(),
            personal_root_seed=b"\x55" * 32, now=1_790_000_000_000,
        )
        event = make_event(
            KeyPair.generate(),
            {"type": "role.define", "name": "reader", "scope_set": ["read"],
             "claim_requires": "self", "version": 1},
            list(store.ledger.heads()), HLC(1_790_000_000_001, 0),
        )
        store.append(event)
        assert event.event_id in {e.event_id for e in store.events()}
    with LedgerStore(activated) as reopened:
        assert event.event_id in {e.event_id for e in reopened.events()}
