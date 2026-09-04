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


def _rename_client(monkeypatch, *, active_ids=("11" * 32,)):
    from types import SimpleNamespace

    monkeypatch.setattr(
        api.api_auth, "require_global_api_authority", lambda request: None
    )
    from tools.network import fleet_machine_profile, fleet_roster, fleet_tunnel_server

    stored = {}
    monkeypatch.setattr(
        fleet_tunnel_server, "_personal_root_pub", lambda: "ab" * 32
    )
    monkeypatch.setattr(
        fleet_roster,
        "current_roster",
        lambda root_pub, org=None: {
            f"pub-{machine_id}": SimpleNamespace(machine_id=machine_id)
            for machine_id in active_ids
        },
    )
    monkeypatch.setattr(
        fleet_machine_profile,
        "store",
        lambda machine_id, name: stored.update({machine_id: name}),
    )
    return TestClient(Starlette(routes=api.routes)), stored


def test_rename_stores_the_label_for_an_active_machine(monkeypatch):
    client, stored = _rename_client(monkeypatch)
    response = client.patch(
        "/api/plugins/fleet/machines/" + "11" * 32 + "/name",
        json={"display_name": "Garage rack"},
    )
    assert response.status_code == 200, response.text
    assert stored == {"11" * 32: "Garage rack"}


def test_rename_refuses_a_machine_outside_the_active_roster(monkeypatch):
    client, stored = _rename_client(monkeypatch, active_ids=())
    response = client.patch(
        "/api/plugins/fleet/machines/" + "11" * 32 + "/name",
        json={"display_name": "Garage rack"},
    )
    assert response.status_code == 404
    assert stored == {}


def test_rename_requires_operator_authority(monkeypatch):
    monkeypatch.setattr(
        api.api_auth,
        "require_global_api_authority",
        lambda request: JSONResponse({"error": "denied"}, status_code=403),
    )
    response = TestClient(Starlette(routes=api.routes)).patch(
        "/api/plugins/fleet/machines/" + "11" * 32 + "/name",
        json={"display_name": "x"},
    )
    assert response.status_code == 403
