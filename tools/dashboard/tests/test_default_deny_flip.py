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


import re as _re
from starlette.routing import Route as _Route


def _api_routes(app):
    """Every ``/api`` Route on the app, as (path, method). Plugin routes are
    extended into the app route list (not mounted), so they appear here too."""
    for r in getattr(app, "routes", []):
        if isinstance(r, _Route) and r.path.startswith("/api/"):
            for m in sorted(r.methods or ["GET"]):
                if m not in ("HEAD", "OPTIONS"):
                    yield r.path, m


def _concrete(path: str) -> str:
    # Fill {param} and {param:path} with a dummy segment. A default-deny route
    # refuses before the handler, so param validity is irrelevant to the refusal.
    return _re.sub(r"\{[^}]+\}", "x", path)


def test_no_api_route_serves_a_no_credential_caller(gate_enforcing, test_app):
    """auto-so9hi, invariant 'no unauth endpoint': with the human gate enforced,
    every ``/api`` route refuses an anonymous caller unless its ``(method, path)``
    is a named public exception. A 2xx to a no-credential client is a leak — the
    handler ran without authentication. Non-2xx (401/403/400/404/405/5xx) all
    mean 'not served'. This sweeps the whole live route table, so a new route is
    covered the moment it is added.
    """
    from tools.dashboard.route_policy import PUBLIC_EXCEPTIONS
    exceptions = set(PUBLIC_EXCEPTIONS)
    leaks = []
    with TestClient(test_app) as client:
        for path, method in _api_routes(test_app):
            if (method, path) in exceptions:
                continue
            resp = client.request(method, _concrete(path))
            if resp.status_code < 400:
                leaks.append(f"{method} {path} -> {resp.status_code}")
    assert not leaks, (
        "these /api routes served a NO-CREDENTIAL caller (leak, or a genuinely "
        "public route missing from route_policy.PUBLIC_EXCEPTIONS — classify and "
        "add it there with a reason):\n  " + "\n  ".join(sorted(leaks))
    )
