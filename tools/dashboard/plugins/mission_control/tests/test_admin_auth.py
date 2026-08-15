"""Global-operator authorization for Mission Control administration."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth
from tools.dashboard.plugins.mission_control.entrypoints import api as mc_api


COOKIE = "test_dashboard_session"


def _authenticate_bearer(request):
    identities = {
        "Bearer org-a": ("agent-a", "org-a"),
        "Bearer local": ("host-local", None),
    }
    identity = identities.get(request.headers.get("authorization", ""))
    if identity is not None:
        return identity, None
    return None, JSONResponse({"error": "invalid bearer"}, status_code=401)


def _verify_cookie(value):
    return {"sid": "browser-1"} if value == "valid-cookie" else None


@pytest.fixture
def admin_client(monkeypatch):
    calls = []

    def delete_mission(mission_id):
        calls.append(("delete_mission", mission_id))
        return True

    def delete_pillar(pillar_id):
        calls.append(("delete_pillar", pillar_id))
        return True

    def store_avatar(value, display_name):
        calls.append(("store_avatar", value, display_name))
        return None, None

    def create_visitor_token(display_name, *, avatar_attachment_id=None):
        calls.append(("create_visitor_token", display_name, avatar_attachment_id))
        return {
            "token": "test-secret",
            "participant_id": "guest:test",
            "display_name": display_name,
            "avatar_attachment_id": avatar_attachment_id,
        }

    monkeypatch.setattr(mc_api.db, "delete_mission", delete_mission)
    monkeypatch.setattr(mc_api.db, "delete_pillar", delete_pillar)
    monkeypatch.setattr(mc_api, "_store_avatar", store_avatar)
    monkeypatch.setattr(mc_api.db, "create_visitor_token", create_visitor_token)

    app = Starlette(
        routes=[
            Route(
                "/api/missions/{mission_id}",
                mc_api.delete_mission,
                methods=["DELETE"],
            ),
            Route(
                "/api/pillars/{pillar_id}",
                mc_api.delete_pillar,
                methods=["DELETE"],
            ),
            Route(
                "/api/visitor-tokens",
                mc_api.create_visitor_token,
                methods=["POST"],
            ),
        ],
        middleware=[
            Middleware(
                api_auth.ApiIdentityMiddleware,
                authenticate_bearer=_authenticate_bearer,
                verify_cookie=_verify_cookie,
                cookie_name=COOKIE,
            ),
        ],
    )
    with TestClient(app) as client:
        yield client, calls


def _request(client, route):
    if route == "mission":
        return client.delete("/api/missions/test-mission")
    if route == "pillar":
        return client.delete("/api/pillars/test-pillar")
    return client.post(
        "/api/visitor-tokens",
        json={"display_name": "Test Visitor"},
    )


@pytest.mark.parametrize("route", ["mission", "pillar", "visitor"])
@pytest.mark.parametrize(
    ("headers", "cookie", "status"),
    [
        ({}, None, 401),
        ({"Authorization": "Bearer org-a"}, None, 403),
        (
            {
                "Authorization": "Bearer org-a",
                "X-Graph-Org": "personal",
            },
            "valid-cookie",
            403,
        ),
    ],
)
def test_non_global_callers_are_refused_before_admin_side_effects(
    admin_client, route, headers, cookie, status,
):
    client, calls = admin_client
    client.headers.update(headers)
    if cookie:
        client.cookies.set(COOKIE, cookie)

    response = _request(client, route)

    assert response.status_code == status
    assert calls == []
    assert "test-secret" not in response.text


@pytest.mark.parametrize("route", ["mission", "pillar", "visitor"])
@pytest.mark.parametrize(
    ("headers", "cookie"),
    [
        ({"X-Graph-Org": "another-org"}, "valid-cookie"),
        ({"Authorization": "Bearer local"}, None),
    ],
)
def test_global_callers_preserve_admin_behavior(
    admin_client, route, headers, cookie,
):
    client, calls = admin_client
    client.headers.update(headers)
    if cookie:
        client.cookies.set(COOKIE, cookie)

    response = _request(client, route)

    assert response.status_code == (201 if route == "visitor" else 200)
    assert calls


# ── Handing a pillar to a different session ───────────────────────
#
# coordinator_session is where a caller's identity comes from:
# get_pillar_by_coordinator resolves a session to the pillar it acts as. So
# the setter, and naming a coordinator at creation, both hand out standing --
# and creation was the way round a gate on the setter alone.


@pytest.fixture
def coordinator_client(monkeypatch):
    calls = []

    def set_pillar_coordinator(pillar_id, session):
        calls.append(("set_pillar", pillar_id, session))
        return True

    def get_pillar(pillar_id):
        return {"pillar_id": pillar_id, "mission_id": "m", "name": "P",
                "color": "#fff", "status": "active", "coordinator_session": "x",
                "created_at": 0, "current_revision_id": None,
                "last_done": None, "last_done_at": None}

    def create_pillar(mission_id, name, coordinator_session, color):
        calls.append(("create_pillar", coordinator_session))
        return get_pillar("new-pillar")

    monkeypatch.setattr(mc_api.db, "set_pillar_coordinator", set_pillar_coordinator)
    monkeypatch.setattr(mc_api.db, "get_pillar", get_pillar)
    monkeypatch.setattr(
        mc_api.db, "get_mission",
        lambda mid: {"mission_id": mid, "coordinator_session": "agent-a"})
    monkeypatch.setattr(mc_api.db, "create_pillar", create_pillar)
    monkeypatch.setattr(mc_api, "_pillar_payload", lambda p: p)

    app = Starlette(
        routes=[
            Route("/api/pillars/{pillar_id}/coordinator",
                  mc_api.set_pillar_coordinator, methods=["POST"]),
            Route("/api/missions/{mission_id}/pillars",
                  mc_api.create_pillar, methods=["POST"]),
        ],
        middleware=[
            Middleware(
                api_auth.ApiIdentityMiddleware,
                authenticate_bearer=_authenticate_bearer,
                verify_cookie=_verify_cookie,
                cookie_name=COOKIE,
            ),
        ],
    )
    with TestClient(app) as client:
        yield client, calls


@pytest.mark.parametrize(
    ("headers", "cookie", "status"),
    [
        ({}, None, 401),
        ({"authorization": "Bearer org-a"}, None, 403),
        ({"authorization": "Bearer local"}, None, 200),
    ],
)
def test_only_global_authority_may_hand_a_pillar_to_another_session(
    coordinator_client, headers, cookie, status,
):
    client, calls = coordinator_client
    if cookie:
        client.cookies.set(COOKIE, cookie)
    r = client.post("/api/pillars/p1/coordinator",
                    json={"coordinator_session": "auto-somebody-else"},
                    headers=headers)
    assert r.status_code == status
    assert bool(calls) is (status == 200)


def test_an_org_session_may_name_only_itself_at_creation(
    coordinator_client, monkeypatch,
):
    """Creation was the way round the setter's gate: make a pillar naming
    somebody else and their traffic resolves to it. This caller does not run
    the mission it is adding to, so the only name it may write is its own."""
    client, calls = coordinator_client
    monkeypatch.setattr(
        mc_api.db, "get_mission",
        lambda mid: {"mission_id": mid,
                     "coordinator_session": "somebody-elses-mission"})

    refused = client.post(
        "/api/missions/m1/pillars",
        json={"name": "P", "coordinator_session": "auto-somebody-else"},
        headers={"authorization": "Bearer org-a"},
    )
    assert refused.status_code == 403
    assert calls == []

    allowed = client.post(
        "/api/missions/m1/pillars",
        json={"name": "P", "coordinator_session": "agent-a"},
        headers={"authorization": "Bearer org-a"},
    )
    assert allowed.status_code == 201
    assert calls == [("create_pillar", "agent-a")]


def test_global_authority_may_name_anyone_at_creation(coordinator_client):
    client, calls = coordinator_client
    r = client.post(
        "/api/missions/m1/pillars",
        json={"name": "P", "coordinator_session": "auto-somebody-else"},
        headers={"authorization": "Bearer local"},
    )
    assert r.status_code == 201
    assert calls == [("create_pillar", "auto-somebody-else")]


def test_the_mission_s_own_coordinator_may_staff_its_pillars(coordinator_client):
    """The ordinary way a pillar comes into existence: whoever runs the
    mission adds one and names the session that will run it. get_mission
    here reports agent-a as the mission's coordinator, which is who the
    org-a bearer proves the caller to be."""
    client, calls = coordinator_client
    r = client.post(
        "/api/missions/m1/pillars",
        json={"name": "P", "coordinator_session": "auto-somebody-else"},
        headers={"authorization": "Bearer org-a"},
    )
    assert r.status_code == 201
    assert calls == [("create_pillar", "auto-somebody-else")]
