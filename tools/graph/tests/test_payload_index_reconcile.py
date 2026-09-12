"""Tests for the schema-declared Settings payload-index reconciler (Tier 3).

Spec: graph://7d588dfa-429 §6 + current-code mapping graph://0ee72ca9-99c@4
(Q4). One registry-owned reconciler installs one physical
``(set_id, json_extract(payload,'$.<field>'))`` index per ``(set_id, field)``:

* ``GraphDB.create_org_db`` reconciles every newly created organization store
  before returning — covering ``org_ops.create_org`` and fleet-roster
  ``materialize_org_scopes_from_roster`` — but skips ``slug == "machine"``;
* direct reconciliation is idempotent across fresh, legacy, and already-current
  databases, exposing exactly one declaration-derived index;
* ordinary opens and Settings reads execute no declaration DDL.

The dashboard-startup half of the sweep (existing stores, per-store isolation,
machine exclusion) lives in the dashboard startup test module.
"""

from __future__ import annotations

import pytest

from tools.graph import org_ops, settings_ops
from tools.graph.db import GraphDB, _org_db_path
from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SettingSchema,
    field,
    indexed_payload,
    indexed_payload_declarations,
    reconcile_payload_indexes,
    _payload_index_name,
)

RECON_SET_ID = "autonomy.test.recon"


@pytest.fixture(autouse=True)
def _isolate_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def recon_schema():
    """Register a test schema declaring one indexed payload field."""
    @indexed_payload("k")
    class _ReconTest(SettingSchema):
        set_id = RECON_SET_ID
        schema_revision = 1
        k: str = field(description="indexed key")
        v: str = field(description="value")

    return _ReconTest


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    return root


def _payload_index_names(db) -> set[str]:
    return {
        r[0]
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_settings_payload_%'"
        ).fetchall()
    }


def _expected_names() -> set[str]:
    return {
        _payload_index_name(set_id, f)
        for set_id, f in indexed_payload_declarations()
    }


# ── create_org_db (fresh) ────────────────────────────────────


def test_create_org_db_installs_declared_index(orgs_root, recon_schema):
    db = GraphDB.create_org_db("alpha", type_="shared")
    try:
        names = _payload_index_names(db)
    finally:
        db.close()
    assert _payload_index_name(RECON_SET_ID, "k") in names
    assert names == _expected_names()


def test_create_org_db_skips_machine_store(orgs_root, recon_schema):
    db = GraphDB.create_org_db("machine", type_="personal")
    try:
        names = _payload_index_names(db)
    finally:
        db.close()
    assert names == set(), "machine store must hold no payload indexes"


# ── org_ops.create_org (post-startup path) ───────────────────


def test_org_ops_create_org_installs_declared_index(orgs_root, recon_schema):
    org_ops.create_org(
        "beta", type_="shared", identity_payload={"name": "Beta"},
    )
    db = GraphDB.open_org_db("beta", mode="rw")
    try:
        assert _payload_index_name(RECON_SET_ID, "k") in _payload_index_names(db)
    finally:
        db.close()


# ── materialize_org_scopes_from_roster (post-startup path) ───


def test_materialize_from_roster_installs_declared_index(orgs_root, recon_schema):
    from tools.network import fleet_org_roster
    from tools.network.fleet_sync_scheduler import (
        materialize_org_scopes_from_roster,
    )

    # The roster publish lands in the personal store; create it first.
    orgs_root.mkdir(parents=True, exist_ok=True)
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.close_all_pooled()

    assert fleet_org_roster.publish_org("gamma", "org-uuid-gamma") is True
    created = materialize_org_scopes_from_roster()
    assert "gamma" in created

    db = GraphDB.open_org_db("gamma", mode="rw")
    try:
        assert _payload_index_name(RECON_SET_ID, "k") in _payload_index_names(db)
    finally:
        db.close()


# ── Direct reconcile: legacy, already-current, idempotent ────


def test_reconcile_on_legacy_database(tmp_path, recon_schema):
    # A plain GraphDB open runs _init_schema but no reconcile — a pre-feature
    # store. Reconciling it installs the declared index.
    path = tmp_path / "legacy.db"
    db = GraphDB(path)
    try:
        assert _payload_index_names(db) == set()
        installed = reconcile_payload_indexes(db)
        assert _payload_index_name(RECON_SET_ID, "k") in installed
        assert _payload_index_name(RECON_SET_ID, "k") in _payload_index_names(db)
    finally:
        db.close()


def test_reconcile_is_idempotent(tmp_path, recon_schema):
    path = tmp_path / "repeat.db"
    db = GraphDB(path)
    try:
        reconcile_payload_indexes(db)
        before = _payload_index_names(db)
        # Repeated reconciliation creates no duplicate.
        reconcile_payload_indexes(db)
        reconcile_payload_indexes(db)
        assert _payload_index_names(db) == before
        assert len(before) == len(_expected_names())
    finally:
        db.close()


def test_reconcile_already_current_is_noop(orgs_root, recon_schema):
    # create_org_db already reconciled; reconciling again is a no-op.
    db = GraphDB.create_org_db("delta", type_="shared")
    try:
        before = _payload_index_names(db)
        reconcile_payload_indexes(db)
        assert _payload_index_names(db) == before
    finally:
        db.close()


def test_reconcile_collapses_revisions_to_one_index(tmp_path):
    @indexed_payload("k")
    class _V1(SettingSchema):
        set_id = "autonomy.test.multi"
        schema_revision = 1
        k: str = field(description="k")

    @indexed_payload("k")
    class _V2(SettingSchema):
        set_id = "autonomy.test.multi"
        schema_revision = 2
        k: str = field(description="k")

    path = tmp_path / "multi.db"
    db = GraphDB(path)
    try:
        reconcile_payload_indexes(db)
        names = _payload_index_names(db)
        # One physical index for the (set_id, field) identity across revisions.
        assert _payload_index_name("autonomy.test.multi", "k") in names
        assert sum(
            1 for n in names
            if n == _payload_index_name("autonomy.test.multi", "k")
        ) == 1
    finally:
        db.close()


# ── No declaration DDL on ordinary opens or reads ────────────


def test_ordinary_open_does_not_create_index(tmp_path, recon_schema):
    path = tmp_path / "open.db"
    GraphDB(path).close()  # fresh, no reconcile
    # Reopen: opening must not run declaration DDL.
    db = GraphDB(path)
    try:
        assert _payload_index_names(db) == set()
    finally:
        db.close()


def test_reads_do_not_create_index(orgs_root, recon_schema):
    # A store with every payload index dropped, proving a read never
    # re-installs one. (Under batch collection other plugins' declared indexes
    # may also be present, so drop the whole family rather than one name.)
    org_ops.create_org(
        "epsilon", type_="shared", identity_payload={"name": "E"},
    )
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db("epsilon", mode="rw")
    try:
        for name in _payload_index_names(db):
            db.conn.execute(f'DROP INDEX IF EXISTS "{name}"')
        db.conn.commit()
        assert _payload_index_names(db) == set()
    finally:
        db.close()
        GraphDB.close_all_pooled()

    # An ordinary owned read must not re-create the dropped index.
    settings_ops.read_owned_set(RECON_SET_ID, org="epsilon")
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db("epsilon", mode="ro")
    try:
        assert _payload_index_names(db) == set()
    finally:
        db.close()


def test_reconcile_read_only_handle_is_noop(tmp_path, recon_schema):
    path = tmp_path / "ro.db"
    GraphDB(path).close()
    db = GraphDB(path, mode="ro")
    try:
        assert reconcile_payload_indexes(db) == []
        assert _payload_index_names(db) == set()
    finally:
        db.close()
