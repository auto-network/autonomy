"""auto-3s3gi: the remote API decides the CALLER's authority before it
forwards anything. The receiver runs a forwarded request as this machine --
with the operator's global authority on a fleet machine, as this machine's
member persona on an org runner -- so a caller that lacks that authority
here must be refused here, and forward() never called. A forwarded reply's
headers are filtered on this side too."""

from __future__ import annotations

import base64

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, remote_api, unlock_routes
from tools.dashboard import member_message_client as mmc
from tools.dashboard import session_control_client as scc

MACHINE = "b1" * 32
OPERATOR = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.OPERATOR_COOKIE)


def _org(slug):
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject="auto-x", org=slug)


class Principal:
    """Stands in for ApiIdentityMiddleware: the test names the principal."""

    principal = None

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and Principal.principal is not None:
            scope.setdefault("state", {})["api_principal"] = Principal.principal
        await self.app(scope, receive, send)


def _app():
    from tools.dashboard import server

    return Starlette(routes=[
        Route("/api/dispatch/limits", server.api_dispatch_limits_get),
        Route("/api/terminal/{id}/kill", server.api_terminal_kill, methods=["POST"]),
        Route("/api/session/{project}/{session_id}/tail", server.api_session_tail),
        Route("/api/session/send", server.api_session_send, methods=["POST"]),
        Route("/api/session/create", server.api_session_create, methods=["POST"]),
    ], middleware=[Middleware(Principal), Middleware(remote_api.RemoteTargetGuard)])


ROUTES = [("GET", "/api/dispatch/limits"), ("POST", "/api/terminal/auto-1/kill"),
          ("GET", "/api/session/p/auto-1/tail"), ("POST", "/api/session/send"),
          ("POST", "/api/session/create")]
REPLY = {"ok": True, "result": {"status": 200, "headers": {"content-type": "application/json"},
                                "body": base64.b64encode(b'{"there": true}').decode()}}


@pytest.fixture
def forwarded(monkeypatch):
    sent = []

    async def fleet_request(machine, op, body=None, *, timeout=15.0, stream=False):
        sent.append(("fleet", machine, body["path"]))
        return REPLY

    async def org_request(target, op, body=None, *, timeout=15.0):
        sent.append(("org", target["org"], body["path"]))
        return REPLY

    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)
    monkeypatch.setattr(scc, "request", fleet_request)
    monkeypatch.setattr(mmc, "request", org_request)
    yield sent
    Principal.principal = None


def _call(method, path, principal):
    Principal.principal = principal
    with TestClient(_app()) as client:
        return client.request(method, path, headers={"X-Autonomy-Machine": "sjc-2"},
                              json={} if method == "POST" else None)


@pytest.mark.parametrize("method, path", ROUTES)
def test_an_org_session_cannot_reach_a_fleet_machine(monkeypatch, forwarded, method, path):
    monkeypatch.setattr(remote_api, "target_of", lambda request: (remote_api.FLEET, MACHINE))
    response = _call(method, path, _org("alpha"))
    assert response.status_code == 403 and forwarded == []


@pytest.mark.parametrize("method, path", ROUTES)
def test_unauthenticated_traffic_cannot_reach_a_fleet_machine(monkeypatch, forwarded, method, path):
    monkeypatch.setattr(remote_api, "target_of", lambda request: (remote_api.FLEET, MACHINE))
    response = _call(method, path, None)
    assert response.status_code == 401 and forwarded == []


@pytest.mark.parametrize("method, path", ROUTES)
def test_the_operator_is_still_forwarded(monkeypatch, forwarded, method, path):
    monkeypatch.setattr(remote_api, "target_of", lambda request: (remote_api.FLEET, MACHINE))
    response = _call(method, path, OPERATOR)
    assert response.status_code == 200 and forwarded == [("fleet", MACHINE, path.split("?")[0])]


def test_an_org_session_reaches_only_its_own_orgs_runner(monkeypatch, forwarded):
    runner = {"org": "x-org", "persona_pub": "c1" * 32, "machine_pub": MACHINE}
    monkeypatch.setattr(remote_api, "target_of", lambda request: (remote_api.ORG, runner))
    assert _call("POST", "/api/session/send", _org("y-org")).status_code == 403
    assert forwarded == []
    assert _call("POST", "/api/session/send", _org("x-org")).status_code == 200
    assert forwarded == [("org", "x-org", "/api/session/send")]


def test_an_unknown_name_needs_authority_before_it_is_named_unknown(monkeypatch, forwarded):
    monkeypatch.setattr(remote_api, "target_of", lambda request: ("unknown", "nowhere"))
    assert _call("GET", "/api/dispatch/limits", _org("alpha")).status_code == 403
    assert _call("GET", "/api/dispatch/limits", OPERATOR).status_code == 404


def test_a_forwarded_reply_keeps_only_allowed_headers(monkeypatch, forwarded):
    async def hostile(machine, op, body=None, *, timeout=15.0, stream=False):
        return {"ok": True, "result": {"status": 200, "body": base64.b64encode(
            b"<script>alert(1)</script>").decode(), "headers": {
                "content-type": "text/html; charset=utf-8", "set-cookie": "s=1",
                "location": "https://evil", "cache-control": "no-store"}}}

    monkeypatch.setattr(scc, "request", hostile)
    monkeypatch.setattr(remote_api, "target_of", lambda request: (remote_api.FLEET, MACHINE))
    response = _call("GET", "/api/dispatch/limits", OPERATOR)
    assert "set-cookie" not in response.headers and "location" not in response.headers
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
