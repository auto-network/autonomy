from __future__ import annotations

from tools.dashboard import agent_test_leases
from tools.graph.db import GraphDB


def test_machine_store_enforces_cross_container_resource_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
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
