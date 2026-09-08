"""A ledger append must succeed on a FLEET-ACTIVATED organization database.

This is the condition every test in the ledger-to-Settings lineage has missed,
and the one that hides a whole class of failure. On a real organization the
``settings`` table carries the fleet-sync capture triggers, and those triggers
call ``fleet_sync_capture_enabled()`` — a function registered only by
``tools.graph.db``. Any code that writes a settings row on a connection that
does not carry it (a bare ``sqlite3.connect``, which is what ``LedgerStore``
opens) fails with ``no such function``, and the operator sees a 500 when they
mint an invitation.

A synthetic store never reproduces this, because a database that was never
fleet-activated has no capture triggers to call the function.

The regression this pins: an append on an activated database stores the event
AND lands its Settings row, on a connection that carries the capture
functions, so the row is captured for replication rather than written past the
triggers. Registering an inert stub of that function on the ledger connection
would make this test pass while telling an actively syncing organization that
capture is off — that is silent, permanent divergence and is not a fix.
"""
from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair
from tools.network.ledger.events import HLC, make_event
from tools.network.ledger.store import LedgerStore, org_ledger_db_path

SLUG = "activatedorg"
ORG_ID = "019c0000-0000-7000-8000-0000000009ab"
NOW_MS = 1_790_000_000_000
INCARNATION = "ab" * 32


@pytest.fixture
def activated_org(tmp_path, monkeypatch):
    """An org database with the fleet-sync capture triggers installed."""
    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db(SLUG, root=orgs, org_id=ORG_ID).close()
    db = GraphDB.for_org(SLUG)
    db.activate_fleet_sync_writers(INCARNATION)
    db.close()
    GraphDB.close_all_pooled()
    yield orgs
    GraphDB.close_all_pooled()


def _capture_triggers_present(path) -> bool:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND sql LIKE '%fleet_sync_capture_enabled%' LIMIT 1"
        ).fetchall()
    finally:
        conn.close()
    return bool(rows)


def test_the_fixture_really_is_fleet_activated(activated_org):
    """Guard the guard: if this fails the test below proves nothing."""
    assert _capture_triggers_present(org_ledger_db_path(SLUG)), (
        "the org database has no capture triggers, so this suite would pass "
        "for the wrong reason"
    )


def test_append_stores_the_event_and_its_settings_row(activated_org):
    root = KeyPair.generate()
    genesis = make_event(
        root,
        {"type": "genesis", "org": ORG_ID, "root_pub": root.public_hex},
        [],
        HLC(NOW_MS, 0),
    )
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        event_id = store.append(genesis)
        assert event_id in store.ledger

    # The event is readable back through a fresh store: it was stored, not
    # merely accepted in memory.
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        assert store.get(event_id).payload["type"] == "genesis"

    # And its Settings row exists, keyed by the event id.
    conn = sqlite3.connect(f"file:{org_ledger_db_path(SLUG)}?mode=ro", uri=True)
    try:
        row = conn.execute(
            'SELECT 1 FROM settings WHERE set_id = ? AND "key" = ?',
            ("autonomy.org.ledger-event", event_id),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, (
        "the event has no Settings row, so peers can never receive it"
    )


def test_the_row_write_is_captured_for_replication(activated_org):
    """The row must go through capture, not past it.

    A write that bypasses the triggers would leave the settings row present
    locally and absent from the replication journal — the failure mode that
    an inert stub of ``fleet_sync_capture_enabled`` would produce silently.
    """
    root = KeyPair.generate()
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        event_id = store.append(make_event(
            root,
            {"type": "genesis", "org": ORG_ID, "root_pub": root.public_hex},
            [],
            HLC(NOW_MS, 0),
        ))

    # Match THIS event's row, not merely "the catalog is non-empty" — a bare
    # count passes as soon as anything else was ever captured. The catalog
    # address is length-prefixed binary, so the event id is matched as BLOB
    # bytes; a LIKE pattern silently matches nothing and would make this
    # assertion pass for the wrong reason (auto-0831-221227, who paid for
    # that lesson with a red run).
    conn = sqlite3.connect(f"file:{org_ledger_db_path(SLUG)}?mode=ro", uri=True)
    try:
        captured = conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog "
            "WHERE instr(address, CAST(? AS BLOB)) > 0",
            (event_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert captured == 1, (
        "the event's settings row was written past the capture triggers: it "
        "exists locally and will never replicate"
    )
