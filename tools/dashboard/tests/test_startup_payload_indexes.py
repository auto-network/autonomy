"""Dashboard startup installs declared Settings payload indexes (Tier 3).

Spec: graph://0ee72ca9-99c@4 (Q4). After plugin registration and
``ensure_bootstrap_orgs``, ``_on_startup`` runs the shared registry reconciler
for every existing writable organization store, off the event loop. These
tests pin:

* an organization store present before boot gains its declared payload index
  after the lifespan runs (the plugin-registered ``AgentTestObservationV1``
  ``run_id`` index is the live proof);
* the machine metadata store is excluded from the sweep;
* one store's reconciliation failure is isolated — the sweep logs it and
  continues installing indexes on the remaining stores.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tools.dashboard.plugins.testing.entrypoints.schemas import (
    OBSERVATION_SET_ID,
)
from tools.graph.db import GraphDB, _org_db_path
from tools.graph.schemas.registry import _payload_index_name


def _payload_index_names(path) -> set[str]:
    db = GraphDB(str(path), mode="ro")
    try:
        return {
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_settings_payload_%'"
            ).fetchall()
        }
    finally:
        db.close()


def test_startup_installs_declared_index_on_existing_org(
    test_app, tmp_path, monkeypatch
):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()

    # An org store exists before boot. Drop the index create_org_db installed
    # so the assertion proves STARTUP re-installed it, not creation.
    autonomy = GraphDB.create_org_db("autonomy")
    org_path = autonomy.db_path
    obs_index = _payload_index_name(OBSERVATION_SET_ID, "run_id")
    autonomy.conn.execute(f'DROP INDEX IF EXISTS "{obs_index}"')
    autonomy.conn.commit()
    autonomy.close()
    GraphDB.close_all_pooled()
    assert obs_index not in _payload_index_names(org_path)

    with TestClient(test_app):
        pass
    GraphDB.close_all_pooled()

    assert obs_index in _payload_index_names(org_path), (
        "startup sweep did not install the declared run_id index on the "
        "pre-existing organization store"
    )


def test_startup_skips_machine_store(test_app, tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()

    with TestClient(test_app):
        pass
    GraphDB.close_all_pooled()

    machine_path = _org_db_path("machine")
    assert machine_path.exists(), "machine store should exist after startup flush"
    assert _payload_index_names(machine_path) == set(), (
        "machine metadata store must not receive payload indexes"
    )


def test_startup_isolates_a_failing_store(test_app, tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()

    # Two org stores; reconciliation will be made to fail for just one.
    good = GraphDB.create_org_db("autonomy")
    good_path = good.db_path
    obs_index = _payload_index_name(OBSERVATION_SET_ID, "run_id")
    good.conn.execute(f'DROP INDEX IF EXISTS "{obs_index}"')
    good.conn.commit()
    good.close()
    bad = GraphDB.create_org_db("anchore")
    bad.conn.execute(f'DROP INDEX IF EXISTS "{obs_index}"')
    bad.conn.commit()
    bad.close()
    GraphDB.close_all_pooled()

    import tools.graph.schemas.registry as registry

    real = registry.reconcile_payload_indexes

    def _flaky(db):
        # Fail only for the "anchore" store; succeed for every other.
        if "anchore" in str(db.db_path):
            raise RuntimeError("boom: simulated per-store failure")
        return real(db)

    monkeypatch.setattr(registry, "reconcile_payload_indexes", _flaky)

    with TestClient(test_app):
        pass
    GraphDB.close_all_pooled()

    # The failing store is isolated; the healthy store was still reconciled.
    assert obs_index in _payload_index_names(good_path)
