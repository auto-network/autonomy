"""Acceptance contract for the plugin starter's organization boundary."""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from tools.dashboard import api_auth
from tools.dashboard.plugins._example.entrypoints import api as example_api
from tools.dashboard.plugins._example.entrypoints.schemas import (
    EXAMPLE_RECORD_SET_ID,
    ExamplePluginRecordV1,
)
from tools.graph.schemas.registry import declared_band, declared_home


COOKIE = "example_dashboard_session"


def _authenticate_bearer(request):
    identity = {
        "Bearer org-a": ("agent-a", "org-a"),
        "Bearer local": ("host-local", None),
    }.get(request.headers.get("authorization", ""))
    return (identity, None) if identity is not None else (None, None)


def _verify_cookie(value):
    return {"sid": "operator"} if value == "operator-cookie" else None


def _app() -> Starlette:
    return Starlette(
        routes=example_api.routes,
        middleware=[Middleware(
            api_auth.ApiIdentityMiddleware,
            authenticate_bearer=_authenticate_bearer,
            verify_cookie=_verify_cookie,
            cookie_name=COOKIE,
        )],
    )


def test_manifest_backend_routes_stay_in_the_plugin_namespace():
    assert example_api.routes
    assert all(
        route.path.startswith("/api/plugins/example/")
        for route in example_api.routes
    )


def test_example_setting_is_organization_homed_and_cannot_read_through():
    assert declared_home(EXAMPLE_RECORD_SET_ID) == "organization"
    assert declared_band(EXAMPLE_RECORD_SET_ID, 1) == ("raw", "raw")
    assert ExamplePluginRecordV1._key_strategy == "fixed:current"


def test_org_bearer_forces_settings_read_to_token_org(monkeypatch):
    calls = []

    def fake_read(set_id, key, *, org, peers):
        calls.append((set_id, key, org, peers))
        return {
            "key": "current",
            "payload": {
                "message": "scoped",
                "updated_at": "2026-08-21T00:00:00Z",
            },
        }

    monkeypatch.setattr(example_api.settings_ops, "read_set_key", fake_read)
    with TestClient(_app()) as client:
        response = client.get(
            "/api/plugins/example/record",
            headers={
                "Authorization": "Bearer org-a",
                "X-Graph-Org": "org-b",
            },
        )

    assert response.status_code == 200
    assert response.json()["organization"] == "org-a"
    assert calls == [(EXAMPLE_RECORD_SET_ID, "current", "org-a", [])]


def test_operator_selection_scopes_the_settings_read(monkeypatch):
    calls = []

    def fake_read(set_id, key, *, org, peers):
        calls.append((set_id, key, org, peers))
        return None

    monkeypatch.setattr(example_api.settings_ops, "read_set_key", fake_read)
    with TestClient(_app()) as client:
        client.cookies.set(COOKIE, "operator-cookie")
        response = client.get(
            "/api/plugins/example/record",
            headers={"X-Graph-Org": "org-b"},
        )

    assert response.status_code == 200
    assert response.json() == {"organization": "org-b", "record": None}
    assert calls == [(EXAMPLE_RECORD_SET_ID, "current", "org-b", [])]


def test_authenticated_operator_without_org_gets_a_bounded_error(monkeypatch):
    def should_not_read(*args, **kwargs):
        raise AssertionError("Settings must not be read without an org scope")

    monkeypatch.setattr(example_api.settings_ops, "read_set_key", should_not_read)
    with TestClient(_app()) as client:
        client.cookies.set(COOKIE, "operator-cookie")
        response = client.get("/api/plugins/example/record")

    assert response.status_code == 400
    assert response.json() == {"error": "organization scope required"}


def test_org_header_without_a_credential_is_not_authentication(
    monkeypatch,
):
    from tools.dashboard import unlock_routes

    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)
    with TestClient(_app()) as client:
        response = client.get(
            "/api/plugins/example/record",
            headers={"X-Graph-Org": "org-a"},
        )

    assert response.status_code == 401
    assert response.json() == {"error": "authentication required"}
