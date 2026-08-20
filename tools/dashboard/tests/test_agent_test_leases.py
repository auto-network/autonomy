from __future__ import annotations

import pytest

from tools.dashboard import agent_test_leases
from tools.graph.db import GraphDB
from tools.graph import settings_ops
from tools.graph.schemas import get_schema
from tools.graph.schemas.agent_test_capacity import DURATION_SET_ID, SCHEMA_REVISION


def test_machine_store_enforces_cross_container_resource_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.close_all_pooled()


def test_duration_history_is_append_only_idempotent_and_capped_per_test(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.close_all_pooled()
    clock = [1_800_000_000.0]
    monkeypatch.setattr(agent_test_leases.time, "time", lambda: clock[0])
    repository = "github.example/acme/project"
    nodeid = "tests/test_widget.py::test_widget"

    schema = get_schema(DURATION_SET_ID, SCHEMA_REVISION)
    assert schema is not None
    assert schema._access_pattern == "append_only_log"
    assert schema._key_strategy == "uuid_v4"

    invalid = agent_test_leases.record_durations(
        "github.example/acme/invalid",
        "invalid-run",
        [
            {"nodeid": nodeid, "duration_seconds": 1.0, "outcome": "passed"},
            {"nodeid": "tests/test_bad.py::test_bad", "duration_seconds": -1.0, "outcome": "passed"},
        ],
    )
    assert invalid["ok"] is False
    assert not [
        member
        for member in settings_ops.read_set(DURATION_SET_ID, org="machine", peers=[]).members
        if member.payload["repository"] == "github.example/acme/invalid"
    ]

    for index in range(12):
        clock[0] += 1
        result = agent_test_leases.record_durations(
            repository,
            f"run-{index}",
            [{"nodeid": nodeid, "duration_seconds": float(index), "outcome": "passed"}],
        )
        assert result["ok"] is True

    members = settings_ops.read_set(DURATION_SET_ID, org="machine", peers=[]).members
    assert len(members) == 10
    assert {member.payload["run_id"] for member in members} == {
        f"run-{index}" for index in range(2, 12)
    }
    with pytest.raises(ValueError, match="append_only_log"):
        settings_ops.upsert_by_key(
            DURATION_SET_ID,
            SCHEMA_REVISION,
            members[0].key,
            members[0].payload,
            org="machine",
        )

    duplicate = agent_test_leases.record_durations(
        repository,
        "run-11",
        [{"nodeid": nodeid, "duration_seconds": 99.0, "outcome": "failed"}],
    )
    assert duplicate["appended"] == 0
    assert duplicate["duplicates"] == 1

    history = agent_test_leases.duration_history(repository, [nodeid])
    assert history["matched_tests"] == 1
    assert len(history["tests"][0]["observations"]) == 10
    assert history["tests"][0]["observations"][0]["run_id"] == "run-11"
    assert history["tests"][0]["median_seconds"] == 6.5

    estimate = agent_test_leases.estimate_duration(
        repository,
        [nodeid, "tests/test_missing.py"],
        parallelism=2,
    )
    assert estimate["estimated_seconds"] == 3.25
    assert estimate["serial_seconds"] == 6.5
    assert estimate["sampled_tests"] == 1
    assert estimate["sample_count"] == 10
    assert estimate["unknown_selectors"] == ["tests/test_missing.py"]
    GraphDB.close_all_pooled()
    clock = [1_800_000_000.0]
    monkeypatch.setattr(agent_test_leases.time, "time", lambda: clock[0])

    first = agent_test_leases.transact("acquire", {
        "lease_id": "session-a:run-a",
        "session": "session-a",
        "run_id": "run-a",
        "resources": {"tests": 10, "browsers": 3},
    })
    test_waiter = agent_test_leases.transact("acquire", {
        "lease_id": "session-b:run-b",
        "session": "session-b",
        "run_id": "run-b",
        "resources": {"tests": 7},
    })
    browser_waiter = agent_test_leases.transact("acquire", {
        "lease_id": "session-c:run-c",
        "session": "session-c",
        "run_id": "run-c",
        "resources": {"browsers": 2, "tests": 1},
    })

    assert first["state"] == "granted"
    assert test_waiter["state"] == "queued"
    assert test_waiter["unavailable"]["tests"] == {"requested": 7, "used": 10, "limit": 16}
    assert browser_waiter["state"] == "queued"
    assert browser_waiter["unavailable"]["browsers"] == {"requested": 2, "used": 3, "limit": 4}
    status = agent_test_leases.transact("status", {})
    assert status["used"] == {"tests": 10, "browsers": 3}
    assert (tmp_path / "machine.db").exists()

    released = agent_test_leases.transact("release", {"lease_id": "session-a:run-a"})
    assert released["state"] == "released"
    admitted = agent_test_leases.transact("acquire", {
        "lease_id": "session-b:run-b",
        "session": "session-b",
        "run_id": "run-b",
        "resources": {"tests": 7},
    })
    assert admitted["state"] == "granted"

    clock[0] += agent_test_leases.DEFAULT_TTL_SECONDS + 1
    expired = agent_test_leases.transact("status", {})
    assert expired["active_leases"] == 0
    assert expired["used"] == {"tests": 0, "browsers": 0}

    agent_test_leases.record_event("session-a", "run_started")
    agent_test_leases.record_event("session-a", "raw_pytest_refused")
    agent_test_leases.record_event("session-b", "run_started")
    telemetry = agent_test_leases.telemetry_status()
    assert telemetry == {
        "ok": True,
        "sessions": 2,
        "counts": {"run_started": 2, "raw_pytest_refused": 1},
    }
    GraphDB.close_all_pooled()
