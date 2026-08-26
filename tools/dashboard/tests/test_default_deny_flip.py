"""The live default-deny wrap: undeclared routes refuse a no-credential caller.

This drives a genuinely anonymous client — a real request through the real
ApiIdentityMiddleware with no cookie and no bearer, so the principal is
COMPATIBILITY because the middleware classified it, not because a fixture
stamped one. test_anonymous_client_is_actually_anonymous proves that, so the
sweep cannot silently regress into measuring an injected principal.
"""

from __future__ import annotations

import asyncio

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


from starlette.routing import Route as _Route


def test_every_api_route_carries_the_default_deny_wrap(test_app):
    """auto-so9hi 'no unauth endpoint', tested at THE ONE PLACE it is enforced.

    ``route_policy.apply_default_deny`` wraps every ``/api`` route by
    construction and stamps the wrapper ``_route_policy_wrapped``. The exception
    check AND the auth refusal both live inside that single wrapper (public
    exceptions are wrapped too — they pass through), so the whole invariant
    reduces to one question: does every ``/api`` route on the assembled app
    carry the marker? A route that does not was added AROUND the choke point,
    which is the only way a no-credential hole can exist.

    This is deliberately a structural pass over the route table, not 320
    anonymous HTTP requests against every handler. We enforce in one place and
    test THAT place: the wrap logic itself is proven once in
    :func:`test_guarded_wrap_refuses_admits_and_serves_exceptions`, the org
    scoping behaviorally in ``test_api_auth_middleware`` /
    ``test_search_org_as_caller``. Re-deriving the refusal per route would test
    the same common function hundreds of times.
    """
    unwrapped = []
    for r in getattr(test_app, "routes", []):
        if isinstance(r, _Route) and r.path.startswith("/api/"):
            if not getattr(
                getattr(r, "endpoint", None), "_route_policy_wrapped", False
            ):
                for m in sorted(r.methods or ["GET"]):
                    if m not in ("HEAD", "OPTIONS"):
                        unwrapped.append(f"{m} {r.path}")
    assert not unwrapped, (
        "these /api routes bypass route_policy.apply_default_deny (added around "
        "the choke point — a no-credential hole):\n  " + "\n  ".join(sorted(unwrapped))
    )


def test_the_structural_check_is_not_vacuous(test_app):
    """Guard the guard: an empty route table would pass the wrap check by
    examining nothing. Assert the population is real."""
    count = sum(
        1 for r in getattr(test_app, "routes", [])
        if isinstance(r, _Route) and r.path.startswith("/api/")
    )
    assert count > 50, f"only {count} /api routes on the app; assembly changed"


def test_guarded_wrap_refuses_admits_and_serves_exceptions(gate_enforcing):
    """The wrap itself, tested ONCE (not per route): a non-exception app route
    refuses an unauthenticated caller, admits an authenticated one, and a
    PUBLIC_EXCEPTION path is served without a credential. This is the single
    enforcement point the structural test trusts."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from tools.dashboard import api_auth, route_policy
    from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind

    ran = {"count": 0}

    async def endpoint(request):
        ran["count"] += 1
        return JSONResponse({"ok": True})

    def _req(path, principal):
        return Request({
            "type": "http", "method": "GET", "path": path, "headers": [],
            "state": {"api_principal": principal},
        })

    anon = api_auth.COMPATIBILITY_PRINCIPAL
    agent = ApiPrincipal(ApiPrincipalKind.ORG_SESSION, subject="auto-1", org="beta")

    guarded = route_policy._guarded(endpoint, "/api/thing", plugin=False)
    assert guarded._route_policy_wrapped is True

    # Unauthenticated → refused, handler never runs.
    resp = asyncio.run(guarded(_req("/api/thing", anon)))
    assert resp.status_code == 401 and ran["count"] == 0
    # Authenticated org session → served.
    asyncio.run(guarded(_req("/api/thing", agent)))
    assert ran["count"] == 1
    # A PUBLIC_EXCEPTION path → served even unauthenticated.
    exc = route_policy._guarded(endpoint, "/api/ping", plugin=False)
    asyncio.run(exc(_req("/api/ping", anon)))
    assert ran["count"] == 2
