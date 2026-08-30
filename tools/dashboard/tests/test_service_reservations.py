"""Red-first Dashboard API contract for sovereign Service reservations.

Design authority: graph://c880c5e6-8bd@3. Bead: auto-otkhu.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, network_routes, unlock_routes
from tools.graph import settings_ops
from tools.graph.db import GraphDB


COOKIE = "test_dashboard_session"
SET_ID = "autonomy.network.namespace-reservation"
REVISION = 1


def _authenticate_bearer(request):
    identities = {
        "Bearer org-a": ("agent-a", "acme"),
        "Bearer local": ("host-local", None),
    }
    identity = identities.get(request.headers.get("authorization", ""))
    if identity is not None:
        return identity, None
    return None, JSONResponse({"error": "invalid bearer"}, status_code=401)


def _verify_cookie(value):
    return {"sid": "browser-1"} if value == "valid-cookie" else None


def _error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"ok": False, "error": code}


@pytest.fixture
def reservation_api(tmp_path, monkeypatch):
    """Real organization-homed Settings with only persona discovery stubbed."""
    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.create_org_db("personal", type_="personal", path=orgs / "personal.db").close()
    GraphDB.create_org_db("acme", type_="shared", path=orgs / "acme.db").close()
    GraphDB.close_all_pooled()

    schema_module = importlib.import_module("tools.graph.schemas.namespace_reservation")
    assert schema_module.NamespaceReservationV1
    service = importlib.import_module("tools.dashboard.service_publication")
    monkeypatch.setattr(
        service,
        "_persona_for_org",
        lambda org: ("00" * 32, "Jérëmy 未来"),
    )
    tick = {"value": 0}

    def now():
        value = datetime(2026, 8, 30, 5, 35, 38, 123000, tzinfo=timezone.utc)
        value += timedelta(seconds=tick["value"])
        tick["value"] += 1
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    monkeypatch.setattr(service, "_utc_now", now)
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)

    events = []

    def capture(*, operation, snapshot, org):
        events.append({"operation": operation, "snapshot": dict(snapshot), "org": org})

    settings_ops.set_emit_hook(capture)
    app = Starlette(
        routes=[
            Route(
                "/api/network/service-reservations",
                network_routes.get_service_reservations,
                methods=["GET"],
            ),
            Route(
                "/api/network/service-reservations",
                network_routes.post_service_reservation,
                methods=["POST"],
            ),
            Route(
                "/api/network/service-reservations/{reservation_id}/state",
                network_routes.put_service_reservation_state,
                methods=["PUT"],
            ),
        ],
        middleware=[
            Middleware(
                api_auth.ApiIdentityMiddleware,
                authenticate_bearer=_authenticate_bearer,
                verify_cookie=_verify_cookie,
                cookie_name=COOKIE,
            )
        ],
    )
    with TestClient(app) as client:
        yield client, events
    settings_ops.set_emit_hook(None)
    GraphDB.close_all_pooled()


def _operator_headers():
    return {"X-Graph-Org": "acme", "Authorization": "Bearer local"}


def _reserve(client, app_label):
    return client.post(
        "/api/network/service-reservations",
        json={"app_label": app_label},
        headers=_operator_headers(),
    )


def _state(client, reservation_id, state):
    return client.put(
        f"/api/network/service-reservations/{reservation_id}/state",
        json={"state": state},
        headers=_operator_headers(),
    )


def _members():
    return settings_ops.read_owned_set(SET_ID, org="acme").members


@pytest.mark.parametrize(
    ("headers", "cookies", "status"),
    [
        ({}, {}, 401),
        ({"Authorization": "Bearer org-a", "X-Graph-Org": "acme"}, {}, 403),
        (
            {"Authorization": "Bearer org-a", "X-Graph-Org": "other"},
            {COOKIE: "valid-cookie"},
            403,
        ),
    ],
)
def test_global_authority_is_checked_before_body_parsing(
    reservation_api, headers, cookies, status
):
    client, _events = reservation_api
    response = client.post(
        "/api/network/service-reservations",
        content=b"not-json",
        headers=headers,
        cookies=cookies,
    )

    assert response.status_code == status
    assert _members() == []


def test_missing_middleware_approved_organization_is_refused(reservation_api):
    client, _events = reservation_api
    response = client.post(
        "/api/network/service-reservations",
        json={"app_label": "docs"},
        headers={"Authorization": "Bearer local"},
    )

    _error(response, 400, "organization_required")
    assert _members() == []


def test_operator_can_reserve_multiple_independent_origins(reservation_api):
    client, events = reservation_api

    docs = _reserve(client, "docs")
    port = _reserve(client, "port-8000")
    assert docs.status_code == 201
    assert port.status_code == 201
    docs_projection = docs.json()["reservation"]
    port_projection = port.json()["reservation"]
    assert docs_projection == {
        "reservation_id": "d3a99356-6161-5507-81c5-71c89f7fdf57",
        "origin": "https://docs.jeremy-66687aadf862bd776c8f.serve.auto.network",
        "persona_label": "jeremy-66687aadf862bd776c8f",
        "app_label": "docs",
        "state": "active",
        "created_at": "2026-08-30T05:35:38.123Z",
        "updated_at": "2026-08-30T05:35:38.123Z",
    }
    assert port_projection["reservation_id"] == "365e4a51-4694-5192-b026-e651f751601e"
    assert port_projection["origin"] == (
        "https://port-8000.jeremy-66687aadf862bd776c8f.serve.auto.network"
    )

    listed = client.get(
        "/api/network/service-reservations", headers=_operator_headers()
    )
    assert listed.status_code == 200
    assert listed.json()["reservations"] == sorted(
        [docs_projection, port_projection], key=lambda row: row["origin"]
    )
    assert len(events) == 2
    for event, key in zip(events, [docs_projection["reservation_id"], port_projection["reservation_id"]]):
        assert event == {
            "operation": "write",
            "snapshot": {
                "set_id": SET_ID,
                "schema_revision": 1,
                "key": key,
                "publication_state": "raw",
                "deprecated": False,
            },
            "org": "acme",
        }

    for member in _members():
        assert member.key in {docs_projection["reservation_id"], port_projection["reservation_id"]}
        assert not ({"reservation_id", "org", "organization", "origin"} & member.payload.keys())


def test_duplicate_and_complete_lifecycle_matrix_is_idempotent(reservation_api):
    client, events = reservation_api
    created = _reserve(client, "port-8000")
    reservation_id = created.json()["reservation"]["reservation_id"]
    assert len(events) == 1

    duplicate = _reserve(client, "port-8000")
    assert duplicate.status_code == 200
    assert duplicate.json() == created.json()
    assert len(events) == 1

    paused = _state(client, reservation_id, "paused")
    assert paused.status_code == 200
    assert paused.json()["reservation"]["state"] == "paused"
    assert len(events) == 2
    same_pause = _state(client, reservation_id, "paused")
    assert same_pause.json() == paused.json()
    assert len(events) == 2

    duplicate_while_paused = _reserve(client, "port-8000")
    assert duplicate_while_paused.json() == paused.json()
    assert len(events) == 2

    resumed = _state(client, reservation_id, "active")
    assert resumed.status_code == 200
    assert resumed.json()["reservation"]["state"] == "active"
    assert len(events) == 3

    released = _state(client, reservation_id, "released")
    assert released.status_code == 200
    assert released.json()["reservation"]["state"] == "released"
    assert released.json()["reservation"]["released_at"] == released.json()["reservation"]["updated_at"]
    assert len(events) == 4
    same_release = _state(client, reservation_id, "released")
    assert same_release.json() == released.json()
    assert len(events) == 4

    _error(_state(client, reservation_id, "active"), 409, "reservation_released")
    _error(_state(client, reservation_id, "paused"), 409, "reservation_released")
    _error(_reserve(client, "port-8000"), 409, "reservation_released")
    assert len(events) == 4


def test_lifecycle_preserves_product_reference_and_sibling(reservation_api):
    client, events = reservation_api
    docs = _reserve(client, "docs").json()["reservation"]
    port = _reserve(client, "port-8000").json()["reservation"]
    row = next(member for member in _members() if member.key == port["reservation_id"])
    settings_ops.upsert_by_key(
        SET_ID,
        REVISION,
        row.key,
        {**row.payload, "product_ref": "package:personal-autonomy"},
        org="acme",
    )
    events.clear()

    _state(client, port["reservation_id"], "paused")
    raw = next(member for member in _members() if member.key == port["reservation_id"])
    assert raw.payload["product_ref"] == "package:personal-autonomy"
    assert "product_ref" not in _state(client, port["reservation_id"], "paused").json()["reservation"]
    listed = client.get(
        "/api/network/service-reservations", headers=_operator_headers()
    ).json()["reservations"]
    assert next(row for row in listed if row["reservation_id"] == docs["reservation_id"]) == docs
    assert len(events) == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "unknown_fields"),
        ({"app_label": "docs", "org": "acme"}, "unknown_fields"),
        ({"app_label": "docs", "persona_pub": "00" * 32}, "unknown_fields"),
        ({"app_label": "docs", "origin": "https://evil.invalid"}, "unknown_fields"),
        ({"app_label": "www"}, "invalid_app_label"),
        ({"app_label": "Uppercase"}, "invalid_app_label"),
    ],
)
def test_create_refuses_authority_fields_and_bad_labels(reservation_api, body, code):
    client, events = reservation_api
    response = client.post(
        "/api/network/service-reservations", json=body, headers=_operator_headers()
    )

    _error(response, 400, code)
    assert events == []
    assert _members() == []


def test_malformed_json_is_stable_and_does_not_write(reservation_api):
    client, events = reservation_api
    response = client.post(
        "/api/network/service-reservations",
        content=b"not-json",
        headers={**_operator_headers(), "Content-Type": "application/json"},
    )

    _error(response, 400, "invalid_json")
    assert events == []


def test_state_route_distinguishes_bad_missing_and_invalid_state(reservation_api):
    client, events = reservation_api
    missing = "11111111-1111-4111-8111-111111111111"

    _error(_state(client, "NOT-A-UUID", "paused"), 400, "invalid_reservation_id")
    _error(_state(client, missing, "paused"), 404, "reservation_not_found")
    _error(_state(client, missing, "other"), 400, "invalid_state")
    response = client.put(
        f"/api/network/service-reservations/{missing}/state",
        json={"state": "paused", "org": "acme"},
        headers=_operator_headers(),
    )
    _error(response, 400, "unknown_fields")
    assert events == []
