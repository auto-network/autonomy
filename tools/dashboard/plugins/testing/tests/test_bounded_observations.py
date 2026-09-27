"""auto-vzujx: Agent Test telemetry and timing reads stay bounded.

The observation set reached 108k rows and every estimate, node estimate and
telemetry event read a whole set per call across ~30 dashboard threads. These
pin that the reads now query only what they need and that growth is capped.
"""
from __future__ import annotations

import pytest

from tools.dashboard.plugins.testing.entrypoints import store
from tools.dashboard.plugins.testing.entrypoints.schemas import (
    OBSERVATION_SET_ID,
    USAGE_SET_ID,
)
from tools.dashboard.plugins.testing.tests.test_where_payload_pushdown import (
    REPO_A, REPO_B, _obs, _run, org,  # noqa: F401  (org is a fixture)
)
from tools.graph import settings_ops


@pytest.fixture
def reads(monkeypatch):
    """Record every set read the store makes: (set_id, where_payload)."""
    seen: list[tuple[str, object]] = []
    real = store._members

    def spy(set_id, org, *, where_payload=None):
        seen.append((set_id, where_payload))
        return real(set_id, org, where_payload=where_payload)
    monkeypatch.setattr(store, "_members", spy)
    return seen


def test_record_event_does_not_read_the_usage_log_below_the_cap(org, reads):
    for event in ("command_plan", "command_status", "run_started"):
        assert store.record_event(org, "auto-t", event)["ok"]
    assert [r for r in reads if r[0] == USAGE_SET_ID] == []


def test_record_event_prunes_once_past_the_cap_plus_slack(org, reads, monkeypatch):
    monkeypatch.setattr(store, "MAX_USAGE_EVENTS", 10)
    for i in range(12):
        store.record_event(org, "auto-t", f"e{i}")
    assert len([r for r in reads if r[0] == USAGE_SET_ID]) == 1
    assert len(settings_ops.read_owned_set(USAGE_SET_ID, org=org).members) == 10


def test_estimates_query_only_matching_nodes(org, reads):
    store.record_run(org, "a-1", _run(REPO_A))
    for nodeid in ("tests/a.py::t1", "tests/a.py::t2", "tests/ab.py::t", "tests/b.py::t"):
        _obs(org, REPO_A, "a-1", nodeid, duration=2.0)
    _obs(org, REPO_B, "b-1", "tests/a.py::t1", duration=9.0)
    reads.clear()

    estimate = store.estimate_duration(org, REPO_A, ["tests/a.py"])
    assert estimate["sampled_tests"] == 2 and estimate["serial_seconds"] == 4.0
    history = store.duration_history(org, REPO_A, ["./tests/"])
    assert history["matched_tests"] == 4
    nodes = store.node_estimates(org, REPO_A, ["tests/b.py::t", "tests/missing.py::t"])
    assert [e["nodeid"] for e in nodes["estimates"]] == ["tests/b.py::t"]
    assert nodes["missing"] == 1

    observation_reads = [where for set_id, where in reads if set_id == OBSERVATION_SET_ID]
    assert observation_reads and all(
        isinstance(where, dict) and "nodeid" in where for where in observation_reads)


def test_observations_are_capped_per_repository_by_newest_runs(org, monkeypatch):
    monkeypatch.setattr(store, "MAX_OBSERVATIONS_PER_REPOSITORY", 2)
    for i in range(4):
        assert store.record_run(org, f"a-{i}", _run(REPO_A, seq=i))["ok"]
        assert _obs(org, REPO_A, f"a-{i}", f"tests/t.py::t{i}")["ok"]
    # The next run's record_run prunes observations beyond the two newest runs.
    assert store.record_run(org, "a-4", _run(REPO_A, seq=4))["ok"]
    runs = {m.payload["run_id"] for m in
            settings_ops.read_owned_set(OBSERVATION_SET_ID, org=org).members}
    assert runs == {"a-3"}  # a-4 and a-3 are the two newest; a-4 has none yet


def _seed_usage(org, count):
    from uuid import uuid4
    from tools.dashboard.plugins.testing.entrypoints.schemas import SCHEMA_REVISION
    return settings_ops.append_log_entries(
        USAGE_SET_ID, SCHEMA_REVISION,
        [(str(uuid4()), {"session": "auto-t", "event": "e", "recorded_at": float(i),
                         "agent_test_version": ""}) for i in range(count)],
        org=org,
    )


def test_prune_commits_ceil_n_over_batch_transactions(org, monkeypatch):
    """Review of 659a90b6: no single write lock over the whole prune."""
    monkeypatch.setattr(store, "PRUNE_BATCH", 2)
    ids = _seed_usage(org, 5)
    calls = []
    real = settings_ops.remove_raw_settings
    monkeypatch.setattr(settings_ops, "remove_raw_settings",
                        lambda batch, *, org: calls.append(list(batch)) or real(batch, org=org))
    assert store._remove_in_batches(ids, org, pause=0) == 5
    assert [len(c) for c in calls] == [2, 2, 1]
    assert settings_ops.read_owned_set(USAGE_SET_ID, org=org).members == []


def test_another_writer_gets_in_between_batches(org, monkeypatch):
    import threading
    monkeypatch.setattr(store, "PRUNE_BATCH", 2)
    ids = _seed_usage(org, 4)
    first_done, writer_done = threading.Event(), threading.Event()
    real = settings_ops.remove_raw_settings
    calls = []

    def batch(batch_ids, *, org):
        removed = real(batch_ids, org=org)  # committed: the db lock is free
        calls.append(len(batch_ids))
        if len(calls) == 1:
            first_done.set()
            assert writer_done.wait(10), "a writer could not get in between batches"
        return removed
    monkeypatch.setattr(settings_ops, "remove_raw_settings", batch)
    pruner = threading.Thread(target=store._remove_in_batches, args=(ids, org),
                              kwargs={"pause": 0})
    pruner.start()
    assert first_done.wait(10)
    assert store.record_event(org, "auto-other", "command_status")["ok"]  # store lock + db write
    writer_done.set()
    pruner.join(10)
    assert calls == [2, 2]


def test_an_id_already_pruned_is_skipped_not_fatal(org):
    ids = _seed_usage(org, 3)
    settings_ops.remove_raw_settings([ids[1]], org=org)
    assert store._remove_batch(ids, org) == 2
    assert settings_ops.read_owned_set(USAGE_SET_ID, org=org).members == []


def test_a_large_prune_runs_in_the_background_once_per_org(org, monkeypatch):
    import threading
    monkeypatch.setattr(store, "PRUNE_BATCH", 2)
    release = threading.Event()
    started = []
    monkeypatch.setattr(store, "_remove_in_batches",
                        lambda ids, org: started.append(len(ids)) or release.wait(10) or 0)
    assert store._prune(["a", "b", "c"], org) == 0      # returns at once
    assert store._prune(["d", "e", "f"], org) == 0      # one already in flight: dropped
    release.set()
    for _ in range(100):
        if org not in store._background_prunes:
            break
        import time
        time.sleep(0.01)
    assert started == [3]
    assert org not in store._background_prunes
