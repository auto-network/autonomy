"""Per-link status and refresh for published Services (auto-q5xni).

Design of record: graph://b2211170-8f3. The contract under test:

* the public HEAD decides Live/Down; any app-level status is Live, the
  gateway codes 502/503/504 and no-answer are Down, and a first-hit
  no-answer with no retry budget is Checking;
* a local link runs all seven stages and names the first failed one;
* a remote link is probed with the HEAD alone and carries no internal stage;
* refresh re-declares the serve host with the connector's serving machine
  and refuses paused and remote links;
* the two routes wire the runner without inventing any of the above.
"""

from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, network_routes, service_publication, service_status, unlock_routes
from tools.dashboard.service_publication import ServicePublicationError

RES_ID = "11111111-1111-4111-8111-111111111111"
LOCAL = "aa" * 32
OTHER = "ff" * 32
HOST = "app.persona-66687aadf862bd776c8f.serve.auto.network"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _head_returning(*outcomes):
    """A HEAD stub answering each call from *outcomes*: an int is a status,
    an exception instance is raised."""
    calls = []
    remaining = list(outcomes)

    def head(host, *, timeout):
        calls.append(host)
        outcome = remaining.pop(0) if remaining else outcomes[-1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    head.calls = calls
    return head


# --- stage 7 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "state"),
    [(200, "Live"), (301, "Live"), (401, "Live"), (404, "Live"), (405, "Live"),
     (500, "Live"), (502, "Down"), (503, "Down"), (504, "Down")],
)
def test_public_head_any_app_answer_is_live_gateway_codes_are_down(status, state):
    result = _run(service_status.probe_public(HOST, head=_head_returning(status), retry_delay=0))
    assert result["state"] == state
    assert result["http_status"] == status
    assert result["attempts"] == 1


def test_public_head_retries_once_then_reports_down():
    head = _head_returning(ssl.SSLError("handshake"), ConnectionRefusedError())
    result = _run(service_status.probe_public(HOST, head=head, retry_delay=0))
    assert result["state"] == "Down"
    assert result["http_status"] is None
    assert result["attempts"] == 2
    assert "unreachable" in result["detail"]


def test_public_head_first_hit_failure_recovers_on_the_retry():
    head = _head_returning(ssl.SSLError("certificate issuing"), 200)
    result = _run(service_status.probe_public(HOST, head=head, retry_delay=0))
    assert result["state"] == "Live"
    assert result["attempts"] == 2


def test_public_head_without_retry_budget_is_checking_not_down():
    head = _head_returning(TimeoutError("first hit"))
    result = _run(service_status.probe_public(HOST, head=head, retries=0))
    assert result["state"] == "Checking"
    assert result["attempts"] == 1


def test_public_head_refuses_an_unvalidated_hostname():
    with pytest.raises(ValueError):
        service_status.head_public_origin("not a host")


# --- the stage runner ---------------------------------------------------


@pytest.fixture
def link(monkeypatch):
    """A reservation + target pair the runner reads, with every external
    seam injectable and the connector's control ops recorded."""
    world = SimpleNamespace(
        state="active", target_machine=LOCAL, local_machine=LOCAL,
        resolve_error=None, cert={"status": "ok"},
        connector={"ok": True, "serving": True, "connector_instance": "c-1",
                   "serving_slot": {"persona_pub": "00" * 32, "machine": "bb" * 32}},
        leases={RES_ID: {"host": HOST, "expires_at": 4102444800, "leased": True}},
        gateway={"state": "healthy", "reason": "ready", "advertised_routes": [RES_ID]},
        serve_host_replies=[{"ok": True}], calls=[], ensured=[],
    )

    def reservation_for_target(org, key, *, serving=False):
        assert org == "acme" and key == RES_ID
        return SimpleNamespace(key=key, payload={
            "persona_pub": "00" * 32, "persona_label": "persona-66687aadf862bd776c8f",
            "app_label": "app", "state": world.state,
        })

    def target_member(org, key):
        if world.target_machine is None:
            return None
        return SimpleNamespace(key=key, payload={
            "machine_id": world.target_machine, "session_id": "session-a",
            "container_id": "cc" * 32, "port": 8000,
        })

    async def resolve(org, key):
        if world.resolve_error:
            raise ServicePublicationError(world.resolve_error, 409)
        return SimpleNamespace(session_id="session-a", port=8000)

    monkeypatch.setattr(service_publication, "_reservation_for_target", reservation_for_target)
    monkeypatch.setattr(service_publication, "_target_member_by_key", target_member)
    monkeypatch.setattr(service_publication, "_read_local_machine_id", lambda: world.local_machine)
    monkeypatch.setattr(service_publication, "resolve_service_target", resolve)
    monkeypatch.setattr(service_status, "PUBLIC_RETRY_DELAY_S", 0)

    def control(org, op, args):
        world.calls.append((op, dict(args)))
        if op == "connector-status":
            return world.connector
        if op == "host-leases":
            return {"ok": True, "leases": world.leases}
        if op == "serve-host":
            return world.serve_host_replies.pop(0) if world.serve_host_replies else {"ok": True}
        if op == "release-host":
            return {"ok": True}
        raise AssertionError(op)

    def ensure(org):
        world.ensured.append(org)
        return {"running": True, "reason": "ok"}

    world.control = control
    world.ensure = ensure
    world.seams = dict(
        control=control, serve_cert_state=lambda org: world.cert,
        gateway_status=lambda: world.gateway,
    )
    return world


def test_local_link_with_every_stage_passing_is_live(link):
    head = _head_returning(200)
    result = _run(service_status.service_status("acme", RES_ID, head=head, **link.seams))
    assert result["state"] == "Live"
    assert result["remote"] is False
    assert result["serving_machine"] == LOCAL
    assert result["failed_stage"] is None
    assert [s["name"] for s in result["stages"]] == list(service_status.STAGES)
    assert all(s["ok"] is True for s in result["stages"])
    assert result["public"]["http_status"] == 200
    assert head.calls == [HOST]
    # Read-only: nothing was declared or released.
    assert [op for op, _ in link.calls] == ["connector-status", "host-leases"]


def test_local_link_names_the_first_failed_stage_when_down(link):
    link.cert = {"status": "missing"}
    link.leases = {}
    result = _run(service_status.service_status(
        "acme", RES_ID, head=_head_returning(503), **link.seams,
    ))
    assert result["state"] == "Down"
    assert result["failed_stage"] == "certificate"
    assert result["detail"] == "serving certificate missing"
    by_name = {s["name"]: s for s in result["stages"]}
    assert by_name["lease"]["ok"] is False
    assert by_name["public"]["ok"] is False


def test_local_link_target_failure_is_the_named_stage(link):
    link.resolve_error = "target_port_unreachable"
    result = _run(service_status.service_status(
        "acme", RES_ID, head=_head_returning(ConnectionRefusedError()), **link.seams,
    ))
    assert result["state"] == "Down"
    assert result["failed_stage"] == "target"
    assert result["detail"] == "target port unreachable"


def test_local_link_with_no_target_is_down_at_target(link):
    link.target_machine = None
    result = _run(service_status.service_status(
        "acme", RES_ID, head=_head_returning(502), **link.seams,
    ))
    assert result["state"] == "Down"
    assert result["serving_machine"] is None
    assert result["remote"] is False
    assert result["failed_stage"] == "target"


def test_connector_down_skips_the_lease_read_and_is_named(link):
    link.connector = {"ok": True, "serving": False}
    result = _run(service_status.service_status(
        "acme", RES_ID, head=_head_returning(TimeoutError(), TimeoutError()), **link.seams,
    ))
    assert result["state"] == "Down"
    assert result["failed_stage"] == "connector"
    by_name = {s["name"]: s for s in result["stages"]}
    assert by_name["lease"]["ok"] is None
    assert [op for op, _ in link.calls] == ["connector-status"]


def test_public_head_decides_even_when_an_internal_stage_disagrees(link):
    """The HEAD crossed the real public path; a stale internal read (here the
    gateway's process-local route list) must not turn a reachable link Down.
    The disagreement stays visible as the failed stage."""
    link.gateway = {"state": "stopped", "reason": "no-publications", "advertised_routes": []}
    result = _run(service_status.service_status(
        "acme", RES_ID, head=_head_returning(200), **link.seams,
    ))
    assert result["state"] == "Live"
    assert result["failed_stage"] == "gateway"


def test_paused_link_is_paused_and_never_probed(link):
    link.state = "paused"
    head = _head_returning(200)
    result = _run(service_status.service_status("acme", RES_ID, head=head, **link.seams))
    assert result["state"] == "Paused"
    assert result["public"] is None
    assert head.calls == []
    assert link.calls == []


def test_remote_link_is_the_public_head_alone_with_no_internal_stage(link):
    link.target_machine = OTHER
    head = _head_returning(404)
    result = _run(service_status.service_status("acme", RES_ID, head=head, **link.seams))
    assert result["state"] == "Live"
    assert result["remote"] is True
    assert result["serving_machine"] == OTHER
    assert [s["name"] for s in result["stages"]] == ["reservation", "public"]
    assert link.calls == []

    head = _head_returning(ssl.SSLError("x"), ssl.SSLError("x"))
    result = _run(service_status.service_status("acme", RES_ID, head=head, **link.seams))
    assert result["state"] == "Down"
    assert result["failed_stage"] == "public"


def test_unreadable_local_identity_never_makes_a_bound_link_remote_by_accident(link):
    """A machine that cannot say who it is treats a bound target as remote:
    it must not run local stages (and later a refresh) for a link it cannot
    prove is its own."""
    link.local_machine = None
    result = _run(service_status.service_status(
        "acme", RES_ID, head=_head_returning(200), **link.seams,
    ))
    assert result["remote"] is True
    assert link.calls == []


# --- refresh ---------------------------------------------------------------


def test_refresh_ensures_the_connector_and_redeclares_the_pinned_host(link):
    result = _run(service_status.refresh_service(
        "acme", RES_ID, ensure=link.ensure, head=_head_returning(200), **link.seams,
    ))
    assert result["state"] == "Live"
    assert result["refreshed"] is True
    assert result["declared_machine"] == "bb" * 32
    assert link.ensured == ["acme"]
    assert link.calls == [
        ("connector-status", {}),
        ("serve-host", {"reservation": RES_ID, "host": HOST, "machine": "bb" * 32}),
    ]
    by_name = {s["name"]: s for s in result["stages"]}
    assert by_name["lease"]["ok"] is True
    assert "pinned to this machine" in by_name["lease"]["detail"]


def test_refresh_releases_and_redeclares_when_the_host_is_owned_elsewhere(link):
    link.serve_host_replies = [{"ok": False, "error": "host-owned-elsewhere"}, {"ok": True}]
    result = _run(service_status.refresh_service(
        "acme", RES_ID, ensure=link.ensure, head=_head_returning(200), **link.seams,
    ))
    assert [op for op, _ in link.calls] == [
        "connector-status", "serve-host", "release-host", "serve-host",
    ]
    assert result["state"] == "Live"


def test_refresh_reports_a_relay_refusal_as_the_lease_stage(link):
    link.serve_host_replies = [{"ok": False, "error": "lease-held"}]
    result = _run(service_status.refresh_service(
        "acme", RES_ID, ensure=link.ensure, head=_head_returning(503), **link.seams,
    ))
    assert result["state"] == "Down"
    assert result["failed_stage"] == "lease"
    assert "lease-held" in result["detail"]


def test_refresh_declares_unpinned_when_the_connector_reports_no_machine(link):
    link.connector = {"ok": True, "serving": True, "serving_slot": {"machine": None}}
    result = _run(service_status.refresh_service(
        "acme", RES_ID, ensure=link.ensure, head=_head_returning(200), **link.seams,
    ))
    assert result["declared_machine"] is None
    assert ("serve-host", {"reservation": RES_ID, "host": HOST}) in link.calls


def test_refresh_refuses_a_remote_link_and_a_paused_one(link):
    link.target_machine = OTHER
    with pytest.raises(ServicePublicationError) as refused:
        _run(service_status.refresh_service(
            "acme", RES_ID, ensure=link.ensure, head=_head_returning(200), **link.seams,
        ))
    assert refused.value.code == "target_remote"
    assert refused.value.status_code == 409
    assert link.ensured == [] and link.calls == []

    link.target_machine = LOCAL
    link.state = "paused"
    with pytest.raises(ServicePublicationError) as refused:
        _run(service_status.refresh_service(
            "acme", RES_ID, ensure=link.ensure, head=_head_returning(200), **link.seams,
        ))
    assert refused.value.code == "reservation_paused"


def test_refresh_survives_a_supervisor_fault_and_names_the_connector(link):
    def ensure(org):
        raise RuntimeError("no serving delegate")

    link.connector = {"ok": True, "serving": False}
    result = _run(service_status.refresh_service(
        "acme", RES_ID, ensure=ensure, head=_head_returning(TimeoutError(), TimeoutError()), **link.seams,
    ))
    assert result["state"] == "Down"
    assert result["failed_stage"] == "connector"
    assert "no serving delegate" in result["detail"]


# --- the routes ---------------------------------------------------------


def _authenticate_bearer(request):
    identities = {"Bearer local": ("host-local", None), "Bearer org-a": ("agent-a", "acme")}
    identity = identities.get(request.headers.get("authorization", ""))
    if identity is not None:
        return identity, None
    return None, JSONResponse({"error": "invalid bearer"}, status_code=401)


@pytest.fixture
def status_api(monkeypatch):
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)
    seen = []

    async def fake_status(org, reservation_id):
        seen.append(("status", org, reservation_id))
        if reservation_id == "missing":
            raise ServicePublicationError("reservation_not_found", 404)
        return {"reservation_id": reservation_id, "state": "Live", "remote": False}

    async def fake_refresh(org, reservation_id):
        seen.append(("refresh", org, reservation_id))
        if reservation_id == "remote":
            raise ServicePublicationError("target_remote", 409, "served elsewhere")
        return {"reservation_id": reservation_id, "state": "Live", "refreshed": True}

    monkeypatch.setattr(service_status, "service_status", fake_status)
    monkeypatch.setattr(service_status, "refresh_service", fake_refresh)
    app = Starlette(
        routes=[
            Route("/api/network/service-targets/{reservation_id}/status",
                  network_routes.get_service_target_status, methods=["GET"]),
            Route("/api/network/service-targets/{reservation_id}/refresh",
                  network_routes.post_service_target_refresh, methods=["POST"]),
        ],
        middleware=[Middleware(
            api_auth.ApiIdentityMiddleware,
            authenticate_bearer=_authenticate_bearer,
            verify_cookie=lambda value: {"sid": "b"} if value == "valid-cookie" else None,
            cookie_name="test_dashboard_session",
        )],
    )
    with TestClient(app) as client:
        yield client, seen


def _headers():
    return {"X-Graph-Org": "acme", "Authorization": "Bearer local"}


def test_status_and_refresh_routes_are_registered():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in network_routes.ROUTES}
    assert ("/api/network/service-targets/{reservation_id}/status", ("GET", "HEAD")) in paths
    assert ("/api/network/service-targets/{reservation_id}/refresh", ("POST",)) in paths


def test_status_route_is_read_only_and_operator_gated(status_api):
    client, seen = status_api
    assert client.get(f"/api/network/service-targets/{RES_ID}/status").status_code == 401
    refused = client.get(
        f"/api/network/service-targets/{RES_ID}/status",
        headers={"X-Graph-Org": "acme", "Authorization": "Bearer org-a"},
    )
    assert refused.status_code == 403
    assert seen == []
    response = client.get(f"/api/network/service-targets/{RES_ID}/status", headers=_headers())
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "status": {"reservation_id": RES_ID, "state": "Live", "remote": False},
    }
    missing = client.get("/api/network/service-targets/missing/status", headers=_headers())
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "error": "reservation_not_found"}


def test_refresh_route_takes_no_body_and_maps_refusals(status_api):
    client, seen = status_api
    with_body = client.post(
        f"/api/network/service-targets/{RES_ID}/refresh", json={"x": 1}, headers=_headers(),
    )
    assert with_body.status_code == 400
    assert with_body.json() == {"ok": False, "error": "unknown_fields"}
    assert seen == []
    response = client.post(f"/api/network/service-targets/{RES_ID}/refresh", headers=_headers())
    assert response.status_code == 200
    assert response.json()["status"]["refreshed"] is True
    remote = client.post("/api/network/service-targets/remote/refresh", headers=_headers())
    assert remote.status_code == 409
    assert remote.json() == {"ok": False, "error": "target_remote", "detail": "served elsewhere"}
