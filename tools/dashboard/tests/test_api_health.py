"""L2 test for GET /api/health (W6, auto-4uvpx).

Proves the reconciliation-loop degradation state tracked by
``SessionMonitor.get_health()`` is actually reachable over HTTP — the
health endpoint is the surface a dashboard banner (or an external
monitor) would poll.
"""

from __future__ import annotations

from starlette.testclient import TestClient


def test_api_health_reports_healthy_by_default(test_app):
    with TestClient(test_app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()

    assert body["reconcile_failure_streak"] == 0
    assert body["reconcile_degraded"] is False


def test_api_health_surfaces_degraded_state(test_app, monkeypatch):
    """A degraded monitor state (as set by reconciliation_tick failures)
    is visible through the endpoint without a code change on the read side."""
    from tools.dashboard import session_monitor as sm_mod

    monkeypatch.setattr(
        sm_mod.session_monitor, "get_health",
        lambda: {
            "reconcile_failure_streak": 7,
            "reconcile_degraded_since": 12345.0,
            "reconcile_degraded_seconds": 900.0,
            "reconcile_degraded": True,
        },
    )

    with TestClient(test_app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()

    assert body["reconcile_failure_streak"] == 7
    assert body["reconcile_degraded"] is True
