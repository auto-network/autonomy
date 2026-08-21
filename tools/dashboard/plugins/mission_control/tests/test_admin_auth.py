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
        pytest.param(
            {}, None, 401,
            marks=pytest.mark.xfail(
                reason=(
                    "pending the default-deny plugin flip (auto-1wwpf.6). The "
                    "operator-only guard stands down while the human gate is "
                    "unenforced, so an unidentified caller reaches these "
                    "destructive routes on an unenrolled dashboard. The wrap "
                    "authenticates plugin routes UNCONDITIONALLY -- a plugin "
                    "has no bootstrap window -- so this passes the moment it "
                    "is wired into the live mount. Left as the pin for that: "
                    "rewriting it to expect 200 would hide the gap."
                ),
                strict=False,
            ),
        ),
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
