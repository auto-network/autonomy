"""The declared ``run_id`` payload index makes the Tier 2 predicate seek.

``AgentTestObservationV1`` declares ``@indexed_payload("run_id")``, so an org
store created through ``org_ops.create_org`` carries the
``(set_id, json_extract(payload,'$.run_id'))`` expression index. These tests
pin the Q4 measure: a scalar and a finite-IN ``run_id`` predicate produce an
index ``SEARCH`` plan, and the indexed result equals the unindexed predicate
result (dropping the index does not change which members come back).

``repository`` is deliberately NOT indexed — only ``run_id`` is declared.
"""
from __future__ import annotations

import pytest

from tools.dashboard.plugins.testing.entrypoints import store
from tools.dashboard.plugins.testing.entrypoints.schemas import (
    AgentTestObservationV1,
    OBSERVATION_SET_ID,
)
from tools.graph import org_ops, settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    _payload_index_name,
    payload_json_extract_sql,
)


@pytest.fixture
def org(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.close_all_pooled()
    org_ops.create_org(
        "alpha", type_="shared", identity_payload={"name": "Alpha"}, root=root,
    )
    yield "alpha"
    GraphDB.close_all_pooled()


def _run(repository: str, seq: int = 0) -> dict:
    return {
        "repository": repository,
        "session": "auto-test",
        "status": "passed",
        "mode": "run",
        "duration_seconds": 2.5,
        "created_at": f"2026-08-21T00:{seq:02d}:00+00:00",
        "finished_at": f"2026-08-21T01:{seq:02d}:00+00:00",
        "selectors": ["tests/test_widget.py"],
        "collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0,
    }


REPO = "github.test/acme/a"


def _seed(org: str, n: int = 6) -> None:
    for i in range(n):
        assert store.record_run(org, f"run-{i}", _run(REPO, seq=i))["ok"]
        assert store.record_observations(
            org, REPO, f"run-{i}",
            [{"nodeid": f"tests/test.py::t{i}",
              "duration_seconds": 1.0, "outcome": "passed"}],
        )["ok"]


def _obs_index_name() -> str:
    return _payload_index_name(OBSERVATION_SET_ID, "run_id")


def test_observation_store_has_run_id_index(org):
    db = GraphDB.open_org_db(org, mode="ro")
    try:
        names = {
            r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_settings_payload_%'"
            ).fetchall()
        }
    finally:
        db.close()
    assert _obs_index_name() in names
    # repository is low-cardinality and deliberately unindexed.
    assert _payload_index_name(OBSERVATION_SET_ID, "repository") not in names
    assert "run_id" in AgentTestObservationV1._indexed_payload_fields
    assert "repository" not in AgentTestObservationV1._indexed_payload_fields


def test_scalar_run_id_predicate_plan_searches_index(org):
    _seed(org)
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db(org, mode="ro")
    try:
        expr = payload_json_extract_sql("run_id")
        plan = db.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT * FROM settings "
            f"WHERE set_id=? AND {expr}=?",
            (OBSERVATION_SET_ID, "run-1"),
        ).fetchall()
    finally:
        db.close()
    detail = " ".join(str(r[-1]) for r in plan)
    assert "SEARCH" in detail
    assert _obs_index_name() in detail


def test_finite_in_run_id_predicate_plan_searches_index(org):
    _seed(org)
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db(org, mode="ro")
    try:
        expr = payload_json_extract_sql("run_id")
        plan = db.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT * FROM settings "
            f"WHERE set_id=? AND {expr} IN (?, ?)",
            (OBSERVATION_SET_ID, "run-1", "run-2"),
        ).fetchall()
    finally:
        db.close()
    detail = " ".join(str(r[-1]) for r in plan)
    assert "SEARCH" in detail
    assert _obs_index_name() in detail


def test_scalar_result_matches_unindexed(org):
    _seed(org)
    indexed = {
        m.key for m in settings_ops.read_owned_set(
            OBSERVATION_SET_ID, org=org, where_payload={"run_id": "run-3"},
        ).members
    }
    # Drop the index and read again: the result must be identical.
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db(org, mode="rw")
    try:
        db.conn.execute(f'DROP INDEX IF EXISTS "{_obs_index_name()}"')
        db.conn.commit()
    finally:
        db.close()
        GraphDB.close_all_pooled()
    unindexed = {
        m.key for m in settings_ops.read_owned_set(
            OBSERVATION_SET_ID, org=org, where_payload={"run_id": "run-3"},
        ).members
    }
    assert indexed == unindexed
    assert len(indexed) == 1


def test_finite_in_result_matches_unindexed(org):
    _seed(org)
    want = {"run-1", "run-4"}
    indexed = {
        m.payload["run_id"] for m in settings_ops.read_owned_set(
            OBSERVATION_SET_ID, org=org, where_payload={"run_id": list(want)},
        ).members
    }
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db(org, mode="rw")
    try:
        db.conn.execute(f'DROP INDEX IF EXISTS "{_obs_index_name()}"')
        db.conn.commit()
    finally:
        db.close()
        GraphDB.close_all_pooled()
    unindexed = {
        m.payload["run_id"] for m in settings_ops.read_owned_set(
            OBSERVATION_SET_ID, org=org, where_payload={"run_id": list(want)},
        ).members
    }
    assert indexed == unindexed == want
