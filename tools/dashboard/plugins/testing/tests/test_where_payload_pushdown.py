"""The four Agent Test reads now narrow in SQL via ``where_payload``.

`store.py` used to materialize a whole Settings set and filter in Python by
``repository`` (run history, observation members, summary observations) or by
``run_id`` membership (stale-observation pruning). Those prefilters now push a
schema-authorized predicate into :func:`read_owned_set`. These regressions pin
that the migration preserved behavior: repository isolation, run-history
capping, per-node observation retention, stale-run observation pruning, and the
optional-repository summary — output, ordering, dedup, and retention unchanged.
"""
from __future__ import annotations

import pytest

from tools.dashboard.plugins.testing.entrypoints import store
from tools.dashboard.plugins.testing.entrypoints.schemas import (
    OBSERVATION_SET_ID,
    RUN_SET_ID,
)
from tools.graph import org_ops, settings_ops
from tools.graph.db import GraphDB


def _run(repository: str, *, status: str = "passed", seq: int = 0) -> dict:
    # A distinct, increasing finished_at per run so history ordering (and thus
    # which runs are capped out) is deterministic rather than a uuid tiebreak.
    return {
        "repository": repository,
        "session": "auto-test",
        "status": status,
        "mode": "run",
        "duration_seconds": 2.5,
        "created_at": f"2026-08-21T00:{seq // 60:02d}:{seq % 60:02d}+00:00",
        "finished_at": f"2026-08-21T01:{seq // 60:02d}:{seq % 60:02d}+00:00",
        "selectors": ["tests/test_widget.py"],
        "collected": 1,
        "passed": 1 if status == "passed" else 0,
        "failed": 1 if status == "failed" else 0,
        "errors": 0,
        "skipped": 0,
        "new_failures": 0,
        "known_failures": 0,
        "quarantined_failures": 0,
        "parallelism": 1,
        "agent_test_version": "0.4.0",
        "fingerprint": "abc",
        "rerun_of": "",
        "estimated_seconds": 2.0,
        "estimated_low_seconds": 1.5,
        "estimated_high_seconds": 3.0,
        "estimate_complete": True,
        "estimate_sampled_tests": 1,
    }


@pytest.fixture
def org(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.close_all_pooled()
    org_ops.create_org(
        "alpha", type_="shared",
        identity_payload={"name": "Alpha"}, root=root,
    )
    yield "alpha"
    GraphDB.close_all_pooled()


REPO_A = "github.test/acme/a"
REPO_B = "github.test/acme/b"


def _obs(org, repository, run_id, nodeid, *, duration=1.0, outcome="passed"):
    return store.record_observations(
        org, repository, run_id,
        [{"nodeid": nodeid, "duration_seconds": duration, "outcome": outcome}],
    )


def test_record_run_history_cap_is_scoped_to_repository(org, monkeypatch):
    # run history is filtered by repository via where_payload; a run of a
    # different repository must not count against this repository's cap.
    monkeypatch.setattr(store, "MAX_RUNS_PER_REPOSITORY", 3)
    for i in range(5):
        assert store.record_run(org, f"a-{i}", _run(REPO_A, seq=i))["ok"]
    for i in range(2):
        assert store.record_run(org, f"b-{i}", _run(REPO_B, seq=i))["ok"]

    runs = settings_ops.read_owned_set(RUN_SET_ID, org=org).members
    a_runs = {m.key for m in runs if m.payload["repository"] == REPO_A}
    b_runs = {m.key for m in runs if m.payload["repository"] == REPO_B}
    # Only the newest 3 of repo A survive; repo B is untouched by A's cap.
    assert a_runs == {"a-2", "a-3", "a-4"}
    assert b_runs == {"b-0", "b-1"}


def test_stale_run_observations_are_pruned_by_run_id_in(org, monkeypatch):
    monkeypatch.setattr(store, "MAX_RUNS_PER_REPOSITORY", 2)
    # Three runs; observations attached to each. The oldest run becomes stale
    # and its observations must be pruned via the run_id IN predicate.
    for i in range(3):
        assert store.record_run(org, f"a-{i}", _run(REPO_A, seq=i))["ok"]
        assert _obs(org, REPO_A, f"a-{i}", f"tests/test.py::t{i}")["ok"]

    obs = settings_ops.read_owned_set(OBSERVATION_SET_ID, org=org).members
    surviving_runs = {m.payload["run_id"] for m in obs}
    # a-0 was capped out; its observation is gone with it.
    assert surviving_runs == {"a-1", "a-2"}


def test_observation_members_isolate_by_repository(org):
    assert store.record_run(org, "a-1", _run(REPO_A))["ok"]
    assert store.record_run(org, "b-1", _run(REPO_B))["ok"]
    _obs(org, REPO_A, "a-1", "tests/test.py::a")
    _obs(org, REPO_B, "b-1", "tests/test.py::b")

    members = store._observation_members(org, REPO_A)
    assert {m.payload["repository"] for m in members} == {REPO_A}
    assert {m.payload["nodeid"] for m in members} == {"tests/test.py::a"}


def test_per_node_observation_retention_preserved(org, monkeypatch):
    monkeypatch.setattr(store, "MAX_OBSERVATIONS_PER_TEST", 3)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    node = "tests/test.py::t"
    assert store.record_run(org, "a-0", _run(REPO_A))["ok"]
    for i in range(5):
        assert _obs(org, REPO_A, f"a-run-{i}", node, duration=float(i))["ok"]
        clock[0] += 1

    members = store._observation_members(org, REPO_A)
    # Only the newest 3 observations of the node are retained.
    assert len(members) == 3
    assert sorted(m.payload["duration_seconds"] for m in members) == [2.0, 3.0, 4.0]


def test_summary_repository_filter_matches_unfiltered_scope(org, monkeypatch):
    monkeypatch.setattr(
        store.agent_test_leases, "activity_snapshot",
        lambda _org: {
            "ok": True,
            "limits": {"tests": 16, "browsers": 4},
            "used": {"tests": 0, "browsers": 0},
            "available": {"tests": 16, "browsers": 4},
            "active_leases": 0,
            "queued_requests": 0,
            "queued": {"tests": 0, "browsers": 0},
            "running": [],
            "queue": [],
        },
    )
    assert store.record_run(org, "a-1", _run(REPO_A))["ok"]
    assert store.record_run(org, "b-1", _run(REPO_B))["ok"]
    _obs(org, REPO_A, "a-1", "tests/test.py::a")
    _obs(org, REPO_B, "b-1", "tests/test.py::b")

    scoped = store.dashboard_summary(org, repository=REPO_A)
    # The repository-scoped summary observes only repo A's node.
    assert scoped["tests"]["observation_rows"] == 1
    ranked_nodes = {t["nodeid"] for t in scoped["tests"]["slow"]}
    assert ranked_nodes == {"tests/test.py::a"}

    unfiltered = store.dashboard_summary(org)
    assert unfiltered["tests"]["observation_rows"] == 2
    assert set(unfiltered["repositories"]) == {REPO_A, REPO_B}
