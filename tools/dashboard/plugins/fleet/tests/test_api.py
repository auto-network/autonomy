from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tools.dashboard.plugins.fleet.entrypoints import api


def test_fleet_view_requires_global_operator_authority(monkeypatch):
    monkeypatch.setattr(
        api.api_auth,
        "require_global_api_authority",
        lambda request: JSONResponse({"error": "global API authority required"}, status_code=403),
    )
    monkeypatch.setattr(
        api.projection,
        "build_view",
        lambda: (_ for _ in ()).throw(AssertionError("storage must not be read")),
    )
    response = TestClient(Starlette(routes=api.routes)).get(
        "/api/plugins/fleet/view"
    )
    assert response.status_code == 403


def test_fleet_view_returns_the_single_projection(monkeypatch):
    expected = {
        "serverTime": 1,
        "summary": {},
        "machines": [],
        "invitation": {},
        "activity": {},
    }
    monkeypatch.setattr(
        api.api_auth, "require_global_api_authority", lambda request: None
    )
    monkeypatch.setattr(api.projection, "build_view", lambda: expected)
    response = TestClient(Starlette(routes=api.routes)).get(
        "/api/plugins/fleet/view"
    )
    assert response.status_code == 200
    assert response.json() == expected
