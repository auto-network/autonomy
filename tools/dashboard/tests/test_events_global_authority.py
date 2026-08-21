"""/api/events and /api/events/replay are global-authority only.

The SSE stream is an unfiltered cross-org broadcast — every topic (the whole
fleet's session roster, worktrees, approvals) to every subscriber. An
org-stamped agent bearer must not reach it; only naturally-cross-org consumers
(the operator's cookie, local host tooling) may. These tests prove the guard
fires BEFORE the handler subscribes or replays, so an org bearer never even
opens the stream.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request

from tools.dashboard import api_auth
from tools.dashboard import unlock_routes
from tools.dashboard.server import api_events, api_events_replay

ORG = api_auth.ApiPrincipal(
    api_auth.ApiPrincipalKind.ORG_SESSION, subject="auto-1", org="autonomy")


def _request(principal, query: str = "") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/events",
        "query_string": query.encode(),
        "headers": [],
        "state": {"api_principal": principal},
    })


def test_events_refuses_org_bound_bearer():
    resp = asyncio.run(api_events(_request(ORG)))
    assert resp.status_code == 403


def test_events_replay_refuses_org_bound_bearer():
    resp = asyncio.run(api_events_replay(_request(ORG, "from=1&to=2")))
    assert resp.status_code == 403


def test_events_refuses_compatibility_when_gate_enforced(monkeypatch):
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    assert unlock_routes.gate_enforced() is True
    resp = asyncio.run(api_events(_request(api_auth.COMPATIBILITY_PRINCIPAL)))
    assert resp.status_code == 401
