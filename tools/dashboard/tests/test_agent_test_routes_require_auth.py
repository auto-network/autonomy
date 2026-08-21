"""Every machine or organization Agent Test route rejects anonymous callers.

leases/telemetry/durations mutate machine-wide coordination state (slot
acquisition, telemetry, duration history). Under invariant 4 a registered
``/api`` route rejects a no-credential request unless it is on the public
allowlist, and these are not public. Reported calm-and-specific by
auto-0820-170259 during the auth-gate sweep, before the route went live.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient


AGENT_TEST_ROUTES = [
    "/api/agent-test/leases",
    "/api/plugins/testing/telemetry",
    "/api/plugins/testing/durations",
    "/api/plugins/testing/runs",
]


@pytest.fixture
def gate_enforcing(monkeypatch):
    from tools.dashboard import unlock_routes
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    assert unlock_routes.gate_enforced() is True


@pytest.mark.parametrize("url", AGENT_TEST_ROUTES)
def test_no_agent_test_route_accepts_a_caller_without_a_credential(
    gate_enforcing, test_app, url,
):
    with TestClient(test_app) as client:
        response = client.post(url, json={"action": "status"})

    assert response.status_code == 401, (
        f"{url} accepted an unauthenticated POST with {response.status_code}; "
        f"these routes mutate Agent Test state"
    )


def test_testing_summary_rejects_an_unauthenticated_caller(gate_enforcing, test_app):
    with TestClient(test_app) as client:
        response = client.get("/api/plugins/testing/summary")
    assert response.status_code == 401
