"""The remote API (design graph://b76a496e-a2d): @remote routes run on another
machine through one ``api`` op, and the receiver enforces the route's kinds
and session rules before the handler runs."""

from __future__ import annotations

import asyncio
import base64
import json

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, remote_api, route_policy
from tools.dashboard import session_control_client as scc

MACHINE = "b1" * 32
PERSONA = "c1" * 32
OTHER = "d1" * 32


@remote_api.remote("fleet")
async def fleet_only(request: Request):
    return JSONResponse({"ran": "fleet_only"})


@remote_api.remote("fleet", "org", session_field="id", check_is_owner=True)
async def stop(request: Request):
    return JSONResponse({"stopped": request.path_params["id"]})


@remote_api.remote("fleet", "org", session_field="tmux_session")
async def send(request: Request):
    return JSONResponse({"sent": (await request.json())["tmux_session"]})


@remote_api.remote("fleet", "org", check_is_runner=True)
async def launch(request: Request):
    return JSONResponse({"launched": True})


async def plain(request: Request):
    return JSONResponse({"ran": "plain"})


APP = Starlette(routes=[
    Route("/api/limits", fleet_only),
    Route("/api/terminal/{id}/kill", stop, methods=["POST"]),
    Route("/api/session/send", send, methods=["POST"]),
    Route("/api/session/create", launch, methods=["POST"]),
    Route("/api/plain", plain),
], middleware=[Middleware(remote_api.RemoteTargetGuard)])


def _dispatch(method, path, caller, body=b"", headers=None):
    payload = {"method": method, "path": path, "query": "",
               "headers": headers or {}, "body": base64.b64encode(body).decode()}
    return asyncio.run(remote_api.dispatch(APP, payload, caller))


def _result(reply):
    assert reply["ok"], reply
    r = reply["result"]
    return r["status"], json.loads(base64.b64decode(r["body"]))


FLEET = remote_api.RemoteCaller("fleet", machine_pub=MACHINE)
MEMBER = remote_api.RemoteCaller("org", machine_pub=MACHINE, persona=PERSONA, org="alpha")


def _sessions(monkeypatch, rows):
    from tools.dashboard import session_presence
    from tools.dashboard.dao import dashboard_db

    monkeypatch.setattr(dashboard_db, "get_session", lambda name: rows.get(name))
    monkeypatch.setattr(session_presence, "session_org", lambda row: row.get("org"))


def test_a_fleet_caller_runs_a_decorated_route():
    assert _result(_dispatch("GET", "/api/limits", FLEET)) == (200, {"ran": "fleet_only"})


def test_an_undecorated_route_is_refused_on_the_receiver():
    reply = _dispatch("GET", "/api/plain", FLEET)
    assert reply["refusal"] == remote_api.ROUTE_NOT_REMOTE


def test_a_member_is_refused_a_fleet_only_route():
    assert _dispatch("GET", "/api/limits", MEMBER)["refusal"] == remote_api.SCOPE_REFUSED


def test_stop_is_the_launching_members_only(monkeypatch):
    _sessions(monkeypatch, {
        "auto-mine": {"org": "alpha", "owner_persona": PERSONA},
        "auto-theirs": {"org": "alpha", "owner_persona": OTHER},
        "auto-personal": {"org": None, "owner_persona": None},
    })
    assert _result(_dispatch("POST", "/api/terminal/auto-mine/kill", MEMBER))[0] == 200
    status, body = _result(_dispatch("POST", "/api/terminal/auto-theirs/kill", MEMBER))
    assert (status, body["refusal"]) == (403, remote_api.NOT_OWNER)
    status, body = _result(_dispatch("POST", "/api/terminal/auto-personal/kill", MEMBER))
    assert (status, body["refusal"]) == (404, remote_api.NO_SUCH_SESSION)
    # The operator's own machine skips the session rules.
    assert _result(_dispatch("POST", "/api/terminal/auto-theirs/kill", FLEET))[0] == 200


def test_send_is_any_member_of_the_sessions_org(monkeypatch):
    _sessions(monkeypatch, {"auto-theirs": {"org": "alpha", "owner_persona": OTHER},
                            "auto-else": {"org": "beta", "owner_persona": PERSONA}})
    ok = _dispatch("POST", "/api/session/send", MEMBER,
                   json.dumps({"tmux_session": "auto-theirs"}).encode(),
                   {"content-type": "application/json"})
    assert _result(ok) == (200, {"sent": "auto-theirs"})
    other_org = _dispatch("POST", "/api/session/send", MEMBER,
                          json.dumps({"tmux_session": "auto-else"}).encode())
    assert _result(other_org)[1]["refusal"] == remote_api.NO_SUCH_SESSION


def test_launch_needs_a_live_runner_offer(monkeypatch):
    from tools.dashboard import org_runners

    monkeypatch.setattr(org_runners, "offered_here", lambda slug: False)
    status, body = _result(_dispatch("POST", "/api/session/create", MEMBER))
    assert (status, body["refusal"]) == (403, remote_api.RUNNER_NOT_OFFERED)
    monkeypatch.setattr(org_runners, "offered_here", lambda slug: slug == "alpha")
    assert _result(_dispatch("POST", "/api/session/create", MEMBER)) == (200, {"launched": True})


def test_the_caller_forwards_to_the_named_machine_and_relays_the_reply(monkeypatch):
    sent = []

    async def fake_request(machine, op, body=None, *, timeout=15.0, stream=False):
        sent.append((machine, op, body["method"], body["path"], body["query"]))
        return {"v": 1, "ok": True, "result": {
            "status": 200, "headers": {"content-type": "application/json"},
            "body": base64.b64encode(b'{"there": true}').decode()}}

    monkeypatch.setattr(remote_api, "target_of", lambda request: MACHINE)
    monkeypatch.setattr(scc, "request", fake_request)
    with TestClient(APP) as client:
        response = client.get("/api/limits?_machine=sjc-2&x=1")
    assert response.status_code == 200 and response.json() == {"there": True}
    assert sent == [(MACHINE, "api", "GET", "/api/limits", "x=1")]


def test_no_target_or_this_machine_runs_here(monkeypatch):
    monkeypatch.setattr(remote_api, "target_of", lambda request: None)
    with TestClient(APP) as client:
        assert client.get("/api/limits").json() == {"ran": "fleet_only"}


def test_a_target_on_an_undecorated_route_is_refused_not_run_here():
    with TestClient(APP) as client:
        response = client.get("/api/plain", headers={"X-Autonomy-Machine": "sjc-2"})
    assert (response.status_code, response.json()["refusal"]) == (404, remote_api.ROUTE_NOT_REMOTE)


def test_the_marker_survives_route_policy_wrapping():
    wrapped = route_policy._guarded(stop, "/api/terminal/{id}/kill", plugin=False)
    assert remote_api.rule_of(wrapped) == remote_api.rule_of(stop)


def test_the_identity_middleware_classifies_the_proved_caller():
    middleware = api_auth.ApiIdentityMiddleware(
        APP, authenticate_bearer=lambda *a, **k: None, verify_cookie=lambda *a: None,
        cookie_name="c")

    def classify(caller):
        scope = {"type": "http", "method": "GET", "path": "/api/limits", "headers": [],
                 "query_string": b"", remote_api.SCOPE_KEY: caller}
        return middleware._classify(Request(scope), None)

    fleet, fleet_org = classify(FLEET)
    assert fleet.kind is api_auth.ApiPrincipalKind.REMOTE_MACHINE and fleet.global_authority
    member, member_org = classify(MEMBER)
    assert member.org_bound and not member.global_authority
    assert (member_org, member.persona_id) == ("alpha", PERSONA)
