"""Ledger co-location: org-DB targeting, coexistence, migration, tamper."""

from __future__ import annotations

import sqlite3

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger.errors import SchemaError
from tools.network.ledger.projections import projection_bytes, build_projections
from tools.network.ledger.store import (
    LEDGER_DB_SUFFIX,
    TamperError,
    relocate_ledger_to_org_db,
)

from .conftest import Sim


@pytest.fixture(autouse=True)
def orgs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path))
    return tmp_path


def _populate(store: LedgerStore) -> Sim:
    """A ledger with a role grant and a revocation race (DoD trace)."""
    sim = Sim()
    sim.role_define(sim.root, "member", scope_set=["link:publish"])
    persona = KeyPair.generate()
    sim.role_grant(sim.root, persona, "member")
    d1 = sim.delegate(sim.root, KeyPair.generate(), ["link:publish"])
    base = sorted(sim.ledger.heads())
    sim.revoke_event(sim.root, d1, parents=base)  # concurrent with the grant
    sim.delegate(sim.root, KeyPair.generate(), ["link:revoke"], parents=base)
    store.append_bundle(sim.ledger.events())
    return sim


def test_store_targets_org_db(orgs_dir):
    path = org_ledger_db_path("acme")
    assert path == orgs_dir / "acme.db"
    store = LedgerStore(path)
    try:
        _populate(store)
        tables = {
            r[0]
            for r in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "ledger_meta",
            "ledger_events",
            "ledger_parents",
            "ledger_heads",
            "ledger_projections",
        } <= tables
        store.refresh_projections()
        assert store.load_projections()  # written into the co-located file
    finally:
        store.close()
    assert not (orgs_dir / f"acme{LEDGER_DB_SUFFIX}").exists()


def test_graph_and_ledger_coexist(orgs_dir):
    graphdb_mod = pytest.importorskip("tools.graph.db")
    graph = graphdb_mod.GraphDB.create_org_db("acme", root=orgs_dir)
    org_row = graph.conn.execute("SELECT id, slug FROM orgs").fetchone()
    user_version = graph.conn.execute("PRAGMA user_version").fetchone()[0]
    graph.conn.execute(
        "INSERT INTO sources(id, type, title) VALUES ('src-1', 'note', 'seeded')"
    )
    graph.conn.commit()
    graph.close()

    store = LedgerStore(org_ledger_db_path("acme"))
    try:
        _populate(store)
        assert len(store) > 0
    finally:
        store.close()

    reopened = graphdb_mod.GraphDB.open_org_db("acme", root=orgs_dir)
    try:
        assert tuple(
            reopened.conn.execute("SELECT id, slug FROM orgs").fetchone()
        ) == tuple(org_row)
        assert tuple(
            reopened.conn.execute("SELECT id FROM sources WHERE id='src-1'").fetchone()
        ) == ("src-1",)
        assert (
            reopened.conn.execute("PRAGMA user_version").fetchone()[0] == user_version
        )
    finally:
        reopened.close()

    # And the ledger still hydrates cleanly next to the graph tables.
    with LedgerStore(org_ledger_db_path("acme")) as again:
        assert len(again) > 0


def test_migration_relocates_and_matches_fold(orgs_dir):
    legacy_path = orgs_dir / f"acme{LEDGER_DB_SUFFIX}"
    # Build the legacy store with the PRE-co-location table names.
    legacy = LedgerStore(legacy_path)
    _populate(legacy)
    legacy_count = len(legacy)
    legacy_fp = legacy.fold().fingerprint()
    legacy_rendered = {
        name: projection_bytes(p)
        for name, p in build_projections(legacy.fold()).items()
    }
    legacy.close()
    raw = sqlite3.connect(legacy_path)
    with raw:
        for old, new in (
            ("ledger_events", "events"),
            ("ledger_parents", "parents"),
            ("ledger_heads", "heads"),
            ("ledger_projections", "projections"),
            ("ledger_meta", "meta"),
        ):
            raw.execute(f"ALTER TABLE {old} RENAME TO {new}")
    raw.close()

    assert relocate_ledger_to_org_db("acme") is True
    assert not legacy_path.exists()
    assert (orgs_dir / f"acme{LEDGER_DB_SUFFIX}.migrated").exists()

    with LedgerStore(org_ledger_db_path("acme")) as store:
        assert len(store) == legacy_count
        assert store.fold().fingerprint() == legacy_fp
        assert store.load_projections() == legacy_rendered

    # Idempotent: no legacy file left, nothing appended.
    assert relocate_ledger_to_org_db("acme") is False
    with LedgerStore(org_ledger_db_path("acme")) as store:
        assert len(store) == legacy_count


def test_migration_absent_or_empty_is_false(orgs_dir):
    assert relocate_ledger_to_org_db("ghost") is False
    empty = orgs_dir / f"blank{LEDGER_DB_SUFFIX}"
    sqlite3.connect(empty).close()  # zero-table file
    assert relocate_ledger_to_org_db("blank") is False
    assert empty.exists()  # left untouched


def test_tamper_survives_relocation(orgs_dir):
    store = LedgerStore(org_ledger_db_path("acme"))
    sim = _populate(store)
    victim = sim.ledger.events()[1].event_id
    store.close()

    raw = sqlite3.connect(org_ledger_db_path("acme"))
    with raw:
        wire = raw.execute(
            "SELECT wire FROM ledger_events WHERE event_id=?", (victim,)
        ).fetchone()[0]
        tampered = bytes(wire[:-1]) + bytes([wire[-1] ^ 1])
        raw.execute(
            "UPDATE ledger_events SET wire=? WHERE event_id=?", (tampered, victim)
        )
    raw.close()
    with pytest.raises(TamperError):
        LedgerStore(org_ledger_db_path("acme"))


def test_sql_check_pins_l8_in_the_org_db(orgs_dir):
    store = LedgerStore(org_ledger_db_path("acme"))
    try:
        _populate(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute(
                "INSERT INTO ledger_events(event_id, event_type, author_key,"
                " hlc_ts, hlc_count, wire) VALUES ('x', 'content.view', 'k', 0, 0, x'00')"
            )
        fake = type("NotEvent", (), {"payload": {"type": "content.view"}})()
        with pytest.raises(SchemaError):
            store.append(fake)
    finally:
        store.close()
