"""The live default-deny wrap: undeclared routes refuse a no-credential caller.

This drives a genuinely anonymous client — a real request through the real
ApiIdentityMiddleware with no cookie and no bearer, so the principal is
COMPATIBILITY because the middleware classified it, not because a fixture
stamped one. test_anonymous_client_is_actually_anonymous proves that, so the
sweep cannot silently regress into measuring an injected principal.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def gate_enforcing(monkeypatch):
    from tools.dashboard import unlock_routes
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    assert unlock_routes.gate_enforced() is True


def test_anonymous_client_is_actually_anonymous(gate_enforcing, test_app):
    # A known authenticated-only route (a generic Settings reader) must return
    # 401 through this client. If it does not, the client is not anonymous and
    # every refusal assertion below would be vacuous.
    with TestClient(test_app) as client:
        r = client.get("/api/graph/settings/dashboard.codex.credentials")
    assert r.status_code == 401


def test_a_bootstrap_exception_serves_without_a_credential(gate_enforcing, test_app):
    with TestClient(test_app) as client:
        r = client.get("/api/identity/status")
    assert r.status_code == 200


def test_an_undeclared_app_route_refuses_anonymous(gate_enforcing, test_app):
    # /api/graph/settings/<set_id> is not a bootstrap exception; anonymous 401.
    with TestClient(test_app) as client:
        r = client.get("/api/graph/settings/autonomy.workspace")
    assert r.status_code == 401
