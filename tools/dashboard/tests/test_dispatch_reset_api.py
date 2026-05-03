from __future__ import annotations

from starlette.testclient import TestClient


def test_api_dispatch_reset_reports_noop_when_not_tripped(test_app, monkeypatch):
    from tools.dashboard import server

    monkeypatch.setattr(server, "get_consecutive_failures", lambda bead_id: (0, 0))

    client = TestClient(test_app)
    r = client.post("/api/dispatch/reset/auto-edec1.1")
    assert r.status_code == 200
    assert r.json() == {
        "bead_id": "auto-edec1.1",
        "reset": False,
        "agent_failures": 0,
        "merge_failures": 0,
    }


def test_api_dispatch_reset_clears_failure_streak(test_app, monkeypatch):
    from tools.dashboard import server

    calls = {"count": 0}

    def fake_get_consecutive_failures(bead_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return (3, 1)
        return (0, 0)

    monkeypatch.setattr(server, "get_consecutive_failures", fake_get_consecutive_failures)
    monkeypatch.setattr(
        server,
        "reset_circuit_breaker",
        lambda bead_id: "reset-auto-edec1.1-deadbeef",
    )

    client = TestClient(test_app)
    r = client.post("/api/dispatch/reset/auto-edec1.1")
    assert r.status_code == 200
    assert r.json() == {
        "bead_id": "auto-edec1.1",
        "reset": True,
        "agent_failures": 3,
        "merge_failures": 1,
        "run_id": "reset-auto-edec1.1-deadbeef",
        "agent_failures_after": 0,
        "merge_failures_after": 0,
    }

