"""One publish call performs the whole relay publish, idempotently, and the
status endpoint reports its stages (auto-3q4qn, graph://c9d72ea4-feb §10)."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, network_routes, unlock_routes
from tools.graph import settings_ops
from tools.graph.db import GraphDB

MACHINE_ID = "aa" * 32
DASHBOARD_CID = "0d" * 32
DASHBOARD_IP = "172.30.0.2"
PERSONA = "00" * 32
COOKIE = "dashboard_session"


def _authenticate_bearer(request):
    from starlette.responses import JSONResponse

    if request.headers.get("authorization", "") == "Bearer local":
        return ("host-local", None), None
    return None, JSONResponse({"error": "invalid bearer"}, status_code=401)


def _verify_cookie(value):
    return {"sid": "browser-1"} if value == "valid-cookie" else None


@pytest.fixture
def remote_api(tmp_path, monkeypatch):
    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.create_org_db("personal", type_="personal", path=orgs / "personal.db").close()
    GraphDB.close_all_pooled()
    importlib.import_module("tools.graph.schemas.namespace_reservation")
    importlib.import_module("tools.graph.schemas.service_target")
    importlib.import_module("tools.graph.schemas.dashboard_remote_access")
    service = importlib.import_module("tools.dashboard.service_publication")
    remote_access = importlib.import_module("tools.dashboard.remote_access")
    # The status cache is process-wide; each test starts with a cold one.
    monkeypatch.setattr(remote_access, "_status_cache", None)
    monkeypatch.setattr(remote_access, "_status_lock", None)

    monkeypatch.setattr(service, "_persona_for_org", lambda org: (PERSONA, "Jeremy"))
    monkeypatch.setattr(service, "_read_local_machine_id", lambda: MACHINE_ID)
    monkeypatch.setattr(service, "discover_topology", lambda: SimpleNamespace(network="autonomy_default"))
    containers = {"__dashboard__": service.ContainerInspection(DASHBOARD_CID, DASHBOARD_IP)}

    async def inspect_dashboard(network):
        inspection = containers.get("__dashboard__")
        if inspection is None:
            raise service.ServicePublicationError("dashboard_container_unavailable", 409)
        return inspection

    async def probe(ip, port):
        return (ip, port) == (DASHBOARD_IP, 8081)

    monkeypatch.setattr(service, "_inspect_dashboard_container", inspect_dashboard)
    monkeypatch.setattr(service, "_probe_tcp", probe)
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)
    from tools.dashboard import tls_certificate
    monkeypatch.setattr(tls_certificate, "read_certificate", lambda *a, **k: None)   # no served certificate

    converged = {"reconcile": 0, "reload": 0}
    from tools.dashboard import service_certificate_manager, web_gateway_supervisor, service_status

    monkeypatch.setattr(service_certificate_manager, "request_reconcile",
                        lambda: converged.__setitem__("reconcile", converged["reconcile"] + 1))

    def reload():
        # Counted when requested (the task itself runs later on the loop).
        converged["reload"] += 1

        async def done():
            return {"ok": True}
        return done()
    monkeypatch.setattr(web_gateway_supervisor, "request_reload", reload)
    gateway = {"state": "healthy", "advertised_routes": [], "auth_helpers": []}
    monkeypatch.setattr(web_gateway_supervisor, "status", lambda: dict(gateway))
    certificates: list[dict] = []
    monkeypatch.setattr(service_certificate_manager, "certificate_states", lambda: list(certificates))

    async def link_status(org, reservation_id, **_kw):
        return {"state": "Unavailable", "failed_stage": "certificate",
                "detail": "no certificate", "stages": [{"name": "reservation", "ok": True}]}
    monkeypatch.setattr(service_status, "service_status", link_status)

    app = Starlette(
        routes=[
            Route("/api/network/remote-access/publish", network_routes.post_remote_access_publish, methods=["POST"]),
            Route("/api/network/remote-access/status", network_routes.get_remote_access_status, methods=["GET"]),
        ],
        middleware=[Middleware(api_auth.ApiIdentityMiddleware, authenticate_bearer=_authenticate_bearer,
                               verify_cookie=_verify_cookie, cookie_name=COOKIE)],
    )
    with TestClient(app, base_url="http://localhost:80") as client:
        yield SimpleNamespace(client=client, service=service, remote_access=remote_access,
                              containers=containers, converged=converged, gateway=gateway,
                              certificates=certificates)
    GraphDB.close_all_pooled()


def _headers():
    return {"Authorization": "Bearer local"}


def _publish(client, **body):
    return client.post("/api/network/remote-access/publish", json=body, headers=_headers())


def _reservations():
    from tools.graph.schemas.namespace_reservation import NAMESPACE_RESERVATION_SET_ID
    return settings_ops.read_owned_set(NAMESPACE_RESERVATION_SET_ID, org="personal").members


def _targets():
    from tools.graph.schemas.service_target import SERVICE_TARGET_SET_ID
    return settings_ops.read_owned_set(SERVICE_TARGET_SET_ID, org="personal").members


def test_one_call_publishes_the_dashboard_under_the_personal_persona(remote_api):
    r = _publish(remote_api.client, mode="autonomy")
    assert r.status_code == 200, r.text
    row = r.json()["remote_access"]
    assert row["mode"] == "autonomy" and row["app_label"] == "dashboard"
    assert row["publisher"] == "personal"
    assert row["local_origin"] == "http://localhost"    # links use it until the relay route is live
    assert row["origin"].startswith("https://dashboard.jeremy-") and row["origin"].endswith(".serve.auto.network")
    [reservation] = _reservations()
    assert reservation.key == row["reservation_id"] and reservation.payload["state"] == "active"
    [target] = _targets()
    assert target.payload["kind"] == "dashboard" and target.payload["access_mode"] == "personal"
    assert target.payload["port"] == 8081 and "session_id" not in target.payload
    assert remote_api.converged == {"reconcile": 1, "reload": 1}
    assert remote_api.remote_access.current()["reservation_id"] == row["reservation_id"]


def test_publish_is_idempotent(remote_api):
    first = _publish(remote_api.client, mode="autonomy").json()["remote_access"]
    second = _publish(remote_api.client, mode="autonomy").json()["remote_access"]
    assert second["origin"] == first["origin"] and second["reservation_id"] == first["reservation_id"]
    assert len(_reservations()) == 1 and len(_targets()) == 1


def test_local_and_tailscale_record_the_origin_the_operator_used(remote_api):
    r = _publish(remote_api.client, mode="local")
    assert r.status_code == 200, r.text
    row = r.json()["remote_access"]
    # The origin is the request's own scheme and Host, never body input.
    assert row == {"mode": "local", "origin": "http://localhost", "published_at": row["published_at"]}
    assert _reservations() == [] and _targets() == []
    assert remote_api.remote_access.current()["mode"] == "local"


@pytest.mark.parametrize("mode, host, ok", [
    ("local", "localhost", True), ("local", "127.0.0.1:8088", True), ("local", "192.168.1.20", True),
    ("local", "desktop.local", True), ("local", "dash.example.com", False), ("local", "100.101.1.1", False),
    ("tailscale", "desktop.tail1234.ts.net:8080", True), ("tailscale", "100.101.1.1", True),
    ("tailscale", "localhost", False), ("tailscale", "dash.example.com", False),
    ("local", "[::1]:80", True), ("local", "[fd00::5]:8081", True), ("tailscale", "[fd7a:115c:a1e0::1]", True),
    ("tailscale", "[2001:db8::1]", False),
])
def test_the_recorded_origin_must_fit_the_mode(remote_api, mode, host, ok):
    r = remote_api.client.post("/api/network/remote-access/publish", json={"mode": mode},
                               headers={**_headers(), "Host": host})
    if ok:
        assert r.status_code == 200, r.text
        # IPv6 literals keep their brackets (reviewer): the origin is a link base.
        assert r.json()["remote_access"]["origin"] == f"http://{host.rstrip('.')}"
    else:
        assert r.status_code == 400 and r.json()["error"] == "origin_invalid"


def test_switching_away_from_the_relay_pauses_the_publication(remote_api):
    """Reviewer: local-only must not leave the dashboard reachable remotely."""
    relay = _publish(remote_api.client, mode="autonomy").json()["remote_access"]
    remote_api.converged["reload"] = 0
    local = _publish(remote_api.client, mode="local").json()["remote_access"]
    assert local["paused_relay"] == {"publisher": "personal", "reservation_id": relay["reservation_id"],
                                     "origin": relay["origin"]}
    [reservation] = _reservations()
    assert reservation.payload["state"] == "paused"
    assert remote_api.converged["reload"] == 1
    status = remote_api.client.get("/api/network/remote-access/status", headers=_headers()).json()["status"]
    assert status["mode"] == "local" and status["relay_publication"] == "paused"
    assert status["relay_origin"] == relay["origin"] and "advertised" not in status
    # Choosing the relay again resumes the same reservation.
    again = _publish(remote_api.client, mode="autonomy").json()["remote_access"]
    assert again["reservation_id"] == relay["reservation_id"]
    assert _reservations()[0].payload["state"] == "active" and len(_reservations()) == 1


def test_publish_is_refused_through_the_relay_route(remote_api):
    relay = _publish(remote_api.client, mode="autonomy").json()["remote_access"]
    relay_host = relay["origin"].removeprefix("https://")
    via_host = remote_api.client.post("/api/network/remote-access/publish", json={"mode": "local"},
                                      headers={**_headers(), "Host": relay_host})
    assert via_host.status_code == 403 and via_host.json()["error"] == "through_gateway"
    via_marker = remote_api.client.post("/api/network/remote-access/publish", json={"mode": "local"},
                                        headers={**_headers(), "X-Forwarded-Host": relay_host})
    assert via_marker.status_code == 403
    assert remote_api.remote_access.current()["mode"] == "autonomy"


def test_the_chosen_label_names_the_persona_label_slug(remote_api):
    refused = _publish(remote_api.client, mode="autonomy", label="aut0n0my")
    assert refused.status_code == 400 and refused.json()["error"] == "label_platform_name"
    assert _reservations() == []
    r = _publish(remote_api.client, mode="autonomy", label="boat-lore")
    assert r.status_code == 200, r.text
    assert r.json()["remote_access"]["origin"].startswith("https://dashboard.boat-lore-")
    # Once reserved, the persona's label is bound: another slug is refused,
    # the same slug (or no label) republishes the same origin.
    other = _publish(remote_api.client, mode="autonomy", label="other-name")
    assert other.status_code == 400 and other.json()["error"] == "label_already_bound"
    same = _publish(remote_api.client, mode="autonomy", label="boat-lore")
    assert same.status_code == 200 and same.json()["remote_access"]["origin"] == r.json()["remote_access"]["origin"]


def test_status_is_single_flight_and_cached_briefly(remote_api, monkeypatch):
    from tools.dashboard import service_status

    probes = {"n": 0}

    async def counting(org, reservation_id, **_kw):
        probes["n"] += 1
        return {"state": "Live", "stages": [], "failed_stage": None, "detail": ""}
    monkeypatch.setattr(service_status, "service_status", counting)
    _publish(remote_api.client, mode="autonomy")
    for _ in range(3):
        remote_api.client.get("/api/network/remote-access/status", headers=_headers())
    assert probes["n"] == 1


@pytest.mark.parametrize("body, code", [
    ({"mode": "carrier-pigeon"}, "invalid_mode"),
    ({"mode": "autonomy", "session_id": "x"}, "unknown_fields"),
    ({}, "unknown_fields"),
    ({"mode": "autonomy", "app_label": "Bad Label"}, "invalid_app_label"),
])
def test_refusals_are_exact(remote_api, body, code):
    r = _publish(remote_api.client, **body)
    assert r.status_code == 400 and r.json()["error"] == code


def test_a_stopped_dashboard_container_refuses_and_records_nothing(remote_api):
    remote_api.containers.pop("__dashboard__")
    r = _publish(remote_api.client, mode="autonomy")
    assert r.status_code == 409 and r.json()["error"] == "dashboard_container_unavailable"
    assert remote_api.remote_access.current() is None


def test_status_reports_the_stages_certificate_gate_and_advertisement(remote_api):
    before = remote_api.client.get("/api/network/remote-access/status", headers=_headers()).json()["status"]
    assert before["recorded"] is False and before["mode"] is None and before["tailnet_origin"] is None

    row = _publish(remote_api.client, mode="autonomy").json()["remote_access"]
    status = remote_api.client.get("/api/network/remote-access/status", headers=_headers()).json()["status"]
    assert status["mode"] == "autonomy" and status["origin"] == row["origin"]
    assert status["route_state"] == "Unavailable" and status["failed_stage"] == "certificate"
    assert status["certificate"] == "pending" and status["advertised"] is False
    assert status["gate"] == "pending" and status["enrollment"] == "closed"

    label = row["origin"].split(".", 1)[1].split(".serve.auto.network")[0]
    remote_api.certificates.append({"org": "personal", "persona_label": label, "state": "issuance_failed",
                                    "reason": "Certificate issuance failed: boom"})
    remote_api.gateway["advertised_routes"] = [row["reservation_id"]]
    remote_api.gateway["auth_helpers"] = ["dashboard-passkey"]
    remote_api.remote_access._invalidate_status()   # past the 2 s cache
    status = remote_api.client.get("/api/network/remote-access/status", headers=_headers()).json()["status"]
    assert status["certificate"] == "failed" and "boom" in status["certificate_detail"]
    assert status["advertised"] is True and status["gate"] == "up"


def test_requires_operator_authority(remote_api):
    r = remote_api.client.post("/api/network/remote-access/publish", json={"mode": "local"})
    assert r.status_code in (401, 403)


def test_tailscale_may_name_its_tailnet_origin_explicitly(remote_api, monkeypatch):
    """Chosen from the local address, the request's own origin is localhost;
    the operator names the Tailnet origin, validated as Tailscale. Without a
    Tailnet name in the served certificate it is recorded unverified."""
    from tools.dashboard import tls_certificate

    monkeypatch.setattr(tls_certificate, "read_certificate", lambda *a, **k: None)
    r = _publish(remote_api.client, mode="tailscale", origin="https://desktop.tail1234.ts.net:8080")
    assert r.status_code == 200, r.text
    row = r.json()["remote_access"]
    assert row["origin"] == "https://desktop.tail1234.ts.net:8080" and row["origin_verified"] is False
    status = remote_api.client.get("/api/network/remote-access/status", headers=_headers()).json()["status"]
    assert status["origin_verified"] is False
    bad = _publish(remote_api.client, mode="tailscale", origin="https://dash.example.com")
    assert bad.status_code == 400 and bad.json()["error"] == "origin_invalid"
    other_mode = _publish(remote_api.client, mode="local", origin="http://localhost")
    assert other_mode.status_code == 400 and other_mode.json()["error"] == "unknown_fields"


def test_a_typed_tailnet_name_must_be_this_nodes_certificate(remote_api, monkeypatch):
    """Reviewer: a typo or another node's name would silently become every
    link's base. With a Tailnet name in the served certificate, only that
    name is accepted, and it is recorded verified."""
    from tools.dashboard import tls_certificate

    facts = SimpleNamespace(names=("localhost", "desktop.tail1234.ts.net"), tailnet_name="desktop.tail1234.ts.net")
    monkeypatch.setattr(tls_certificate, "read_certificate", lambda *a, **k: facts)
    wrong = _publish(remote_api.client, mode="tailscale", origin="https://other.tail9999.ts.net:8080")
    assert wrong.status_code == 400 and wrong.json()["error"] == "origin_not_this_node"
    assert "desktop.tail1234.ts.net" in wrong.json().get("detail", "")
    assert remote_api.remote_access.current() is None
    right = _publish(remote_api.client, mode="tailscale", origin="https://desktop.tail1234.ts.net:8080")
    assert right.status_code == 200, right.text
    assert right.json()["remote_access"]["origin_verified"] is True


def test_status_before_recording_carries_what_the_question_needs(remote_api, monkeypatch):
    from tools.dashboard import tls_certificate

    facts = SimpleNamespace(names=("desktop.tail1234.ts.net",), tailnet_name="desktop.tail1234.ts.net")
    monkeypatch.setattr(tls_certificate, "read_certificate", lambda *a, **k: facts)
    monkeypatch.setattr(remote_api.remote_access, "bound_slug", lambda org: "jeremy")
    monkeypatch.setenv("DASHBOARD_PORT", "8080")
    status = remote_api.client.get("/api/network/remote-access/status", headers=_headers()).json()["status"]
    assert status == {"mode": None, "origin": None, "recorded": False,
                      "bound_label": "jeremy", "tailnet_origin": "https://desktop.tail1234.ts.net:8080"}
