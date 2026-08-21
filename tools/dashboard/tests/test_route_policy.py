"""Default-deny wrap: authenticated by construction, open only by declaration.

auto-1wwpf.6. These test the mechanism in isolation (fake routes + fake
principals); the live flip and the whole-surface proof are separate.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.routing import Route

from tools.dashboard import api_auth, route_policy


def _req(method: str, path: str, principal) -> Request:
    scope = {
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"",
        "state": {"api_principal": principal},
    }
    return Request(scope)


async def _ok(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": True})


@pytest.fixture
def gate_enforced(monkeypatch):
    from tools.dashboard import unlock_routes
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    assert unlock_routes.gate_enforced() is True


def _wrap(path, method="GET"):
    return route_policy.apply_default_deny(
        [Route(path, _ok, methods=[method])]
    )[0]


def _wrap_plugin(path, method="GET"):
    return route_policy.apply_default_deny(
        [Route(path, _ok, methods=[method])], plugin=True
    )[0]


async def _call(route, request):
    return await route.endpoint(request)


@pytest.mark.asyncio
async def test_undeclared_route_refuses_unauthenticated(gate_enforced):
    route = _wrap("/api/anything/new")
    compat = api_auth.COMPATIBILITY_PRINCIPAL
    resp = await _call(route, _req("GET", "/api/anything/new", compat))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_undeclared_route_admits_a_bearer_agent(gate_enforced):
    route = _wrap("/api/anything/new")
    agent = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION, subject="s", org="anchore")
    resp = await _call(route, _req("GET", "/api/anything/new", agent))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_undeclared_route_admits_the_operator_cookie(gate_enforced):
    route = _wrap("/api/anything/new")
    op = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="sid")
    resp = await _call(route, _req("GET", "/api/anything/new", op))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_declared_public_exception_passes_unauthenticated(gate_enforced):
    # A real exception entry (identity bootstrap) is served with no credential.
    route = _wrap("/api/identity/status", method="GET")
    compat = api_auth.COMPATIBILITY_PRINCIPAL
    resp = await _call(route, _req("GET", "/api/identity/status", compat))
    assert resp.status_code == 200


def test_every_exception_carries_a_justification():
    for key, reason in route_policy.PUBLIC_EXCEPTIONS.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 20, key


def test_plugins_may_not_be_public_exceptions():
    # No exception path may collide with a plugin route path.
    with pytest.raises(RuntimeError, match="plugin routes may not"):
        route_policy.assert_no_plugin_exceptions({"/api/identity/status"})
    # A disjoint plugin set is fine.
    route_policy.assert_no_plugin_exceptions({"/api/missions/x/site"})


@pytest.fixture
def gate_open(monkeypatch):
    """An UNENROLLED dashboard: the human gate stands down (fail-open)."""
    from tools.dashboard import unlock_routes
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: False)
    assert unlock_routes.gate_enforced() is False


@pytest.mark.asyncio
async def test_app_route_stands_down_when_unenrolled(gate_open):
    """A fresh install must bootstrap: the app guard stands down while the
    gate is not enforced, so a no-credential caller is admitted."""
    route = _wrap("/api/some/app/route")
    compat = api_auth.COMPATIBILITY_PRINCIPAL
    resp = await _call(route, _req("GET", "/api/some/app/route", compat))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_plugin_route_refuses_unauthenticated_even_when_unenrolled(gate_open):
    """An unenrolled dashboard exposes NO plugin routes (operator ruling
    2026-08-21). A plugin route refuses a no-credential caller regardless of
    gate state — no bootstrap window applies to a plugin."""
    route = _wrap_plugin("/api/missions/x/delete")
    compat = api_auth.COMPATIBILITY_PRINCIPAL
    resp = await _call(route, _req("DELETE", "/api/missions/x/delete", compat))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_plugin_route_admits_a_bearer_even_when_unenrolled(gate_open):
    route = _wrap_plugin("/api/missions/x/site", method="POST")
    agent = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION, subject="s", org="anchore")
    resp = await _call(route, _req("POST", "/api/missions/x/site", agent))
    assert resp.status_code == 200
