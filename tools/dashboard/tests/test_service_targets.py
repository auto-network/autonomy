"""Red-first contract for trusted Service reservation target bindings.

Design authority: graph://c880c5e6-8bd@3. Bead: auto-dgj9q.1.
"""

from __future__ import annotations

import importlib
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, network_routes, unlock_routes
from tools.dashboard.dao import dashboard_db
from tools.graph import settings_ops
from tools.graph.db import GraphDB


COOKIE = "test_dashboard_session"
RESERVATION_SET_ID = "autonomy.network.namespace-reservation"
TARGET_SET_ID = "autonomy.network.service-target"
REVISION = 1
ACTIVE_ID = "11111111-1111-4111-8111-111111111111"
PAUSED_ID = "22222222-2222-4222-8222-222222222222"
RELEASED_ID = "33333333-3333-4333-8333-333333333333"
MACHINE_ID = "aa" * 32
CONTAINER_A = "bb" * 32
CONTAINER_A_REPLACED = "cc" * 32
CONTAINER_B = "dd" * 32


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


def _headers(org="acme"):
    return {"X-Graph-Org": org, "Authorization": "Bearer local"}


def _error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"ok": False, "error": code}


def _reservation_payload(state: str, app_label: str) -> dict:
    payload = {
        "persona_pub": "00" * 32,
        "persona_label": "persona-66687aadf862bd776c8f",
        "app_label": app_label,
        "state": state,
        "created_at": "2026-08-30T12:00:00.000Z",
        "updated_at": "2026-08-30T12:00:00.000Z",
    }
    if state == "released":
        payload["released_at"] = "2026-08-30T12:00:00.000Z"
    return payload


@pytest.fixture
def target_api(tmp_path, monkeypatch):
    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.create_org_db("personal", type_="personal", path=orgs / "personal.db").close()
    GraphDB.create_org_db("acme", type_="shared", path=orgs / "acme.db").close()
    GraphDB.create_org_db("other", type_="shared", path=orgs / "other.db").close()
    GraphDB.close_all_pooled()

    importlib.import_module("tools.graph.schemas.namespace_reservation")
    target_schema = importlib.import_module("tools.graph.schemas.service_target")
    assert target_schema.ServiceTargetV1
    service = importlib.import_module("tools.dashboard.service_publication")

    for key, state, label in (
        (ACTIVE_ID, "active", "port-8000"),
        (PAUSED_ID, "paused", "paused-app"),
        (RELEASED_ID, "released", "released-app"),
    ):
        settings_ops.upsert_by_key(
            RESERVATION_SET_ID,
            REVISION,
            key,
            _reservation_payload(state, label),
            org="acme",
        )

    sessions = {
        "session-a": {"tmux_name": "session-a", "type": "container", "project": "acme"},
        "session-b": {"tmux_name": "session-b", "type": "container", "project": "acme"},
        "other-session": {
            "tmux_name": "other-session",
            "type": "container",
            "project": "other",
        },
        "host-session": {"tmux_name": "host-session", "type": "host", "project": "acme"},
        "dead-session": {
            "tmux_name": "dead-session",
            "type": "container",
            "project": "acme",
        },
    }
    live = {name for name in sessions if name != "dead-session"}
    containers = {
        "session-a": service.ContainerInspection(CONTAINER_A, "172.30.0.11"),
        "session-b": service.ContainerInspection(CONTAINER_B, "172.30.0.12"),
        "other-session": service.ContainerInspection("ee" * 32, "172.30.0.13"),
    }
    reachable = {("172.30.0.11", 8000), ("172.30.0.12", 8000)}

    monkeypatch.setattr(dashboard_db, "get_session", lambda name: sessions.get(name))
    monkeypatch.setattr(dashboard_db, "is_session_live", lambda name: name in live)
    monkeypatch.setattr(service, "_read_local_machine_id", lambda: MACHINE_ID)
    monkeypatch.setattr(
        service,
        "discover_topology",
        lambda: SimpleNamespace(network="autonomy_default"),
    )

    async def inspect(session_id, network):
        assert network == "autonomy_default"
        inspection = containers.get(session_id)
        if inspection is None:
            raise service.ServicePublicationError("target_session_unavailable", 409)
        return inspection

    async def probe(ip, port):
        return (ip, port) in reachable

    monkeypatch.setattr(service, "_inspect_session_container", inspect)
    monkeypatch.setattr(service, "_probe_tcp", probe)

    tick = {"value": 0}

    def now():
        value = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        value += timedelta(seconds=tick["value"])
        tick["value"] += 1
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    monkeypatch.setattr(service, "_utc_now", now)
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)

    events = []

    def capture(*, operation, snapshot, org):
        if snapshot.get("set_id") == TARGET_SET_ID:
            events.append({"operation": operation, "snapshot": dict(snapshot), "org": org})

    settings_ops.set_emit_hook(capture)
    app = Starlette(
        routes=[
            Route(
                "/api/network/service-targets",
                network_routes.get_service_targets,
                methods=["GET"],
            ),
            Route(
                "/api/network/service-targets/{reservation_id}",
                network_routes.put_service_target,
                methods=["PUT"],
            ),
            Route(
                "/api/network/service-targets/{reservation_id}",
                network_routes.delete_service_target,
                methods=["DELETE"],
            ),
            Route(
                "/api/network/service-targets/{reservation_id}/check",
                network_routes.check_service_target,
                methods=["POST"],
            ),
            Route(
                "/api/network/service-gateway",
                network_routes.get_service_gateway,
                methods=["GET"],
            ),
            Route(
                "/api/network/published-links",
                network_routes.get_published_links,
                methods=["GET"],
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
        yield client, events, service, containers, reachable
    settings_ops.set_emit_hook(None)
    GraphDB.close_all_pooled()


def _put(client, reservation_id=ACTIVE_ID, session_id="session-a", port=8000, **extra):
    return client.put(
        f"/api/network/service-targets/{reservation_id}",
        json={"session_id": session_id, "port": port, **extra},
        headers=_headers(),
    )


def _check(client, reservation_id=ACTIVE_ID):
    return client.post(
        f"/api/network/service-targets/{reservation_id}/check",
        headers=_headers(),
    )


def _target_members():
    return settings_ops.read_owned_set(TARGET_SET_ID, org="acme").members


def test_service_gateway_status_requires_operator_and_reports_runtime(
    target_api, monkeypatch
):
    client, *_ = target_api
    from tools.dashboard import web_gateway_supervisor

    monkeypatch.setattr(
        web_gateway_supervisor,
        "status",
        lambda: {
            "state": "healthy",
            "reason": "ready",
            "advertised_routes": [ACTIVE_ID],
        },
    )

    refused = client.get("/api/network/service-gateway")
    assert refused.status_code == 401
    response = client.get("/api/network/service-gateway", headers=_headers())
    assert response.status_code == 200
    assert response.json() == {
        "gateway": {
            "state": "healthy",
            "reason": "ready",
            "advertised_routes": [ACTIVE_ID],
        }
    }


class TestServiceTargetApiContract:
    def test_published_links_projects_settings_and_session_title(self, target_api):
        client, _events, *_ = target_api
        _put(client)
        response = client.get("/api/network/published-links", headers=_headers())
        assert response.status_code == 200
        payload = response.json()
        assert payload["shares"] == []
        active = next(
            row for row in payload["services"]
            if row["reservation_id"] == ACTIVE_ID
        )
        assert active["session_title"] == "session-a"
        assert active["target"]["session_id"] == "session-a"
        assert "container_id" not in active["target"]

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
    def test_authority_precedes_body_parsing(self, target_api, headers, cookies, status):
        client, _events, *_ = target_api
        response = client.put(
            f"/api/network/service-targets/{ACTIVE_ID}",
            content=b"not-json",
            headers=headers,
            cookies=cookies,
        )
        assert response.status_code == status
        assert _target_members() == []

    def test_bind_list_check_and_idempotent_unbind(self, target_api):
        client, events, *_ = target_api
        created = _put(client)
        assert created.status_code == 201
        projection = created.json()["target"]
        assert projection == {
            "reservation_id": ACTIVE_ID,
            "session_id": "session-a",
            "port": 8000,
            "created_at": "2026-08-30T12:00:00.000Z",
            "updated_at": "2026-08-30T12:00:00.000Z",
        }
        assert len(events) == 1

        listed = client.get("/api/network/service-targets", headers=_headers())
        assert listed.status_code == 200
        assert listed.json() == {"targets": [projection]}

        checked = _check(client)
        assert checked.status_code == 200
        assert checked.json() == {
            "ok": True,
            "target": {
                "reservation_id": ACTIVE_ID,
                "session_id": "session-a",
                "port": 8000,
                "checked_at": "2026-08-30T12:00:01.000Z",
                "expires_at": "2026-08-30T12:00:06.000Z",
            },
        }

        deleted = client.delete(
            f"/api/network/service-targets/{ACTIVE_ID}", headers=_headers()
        )
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert len(events) == 2
        assert events[-1]["operation"] == "delete"
        repeated = client.delete(
            f"/api/network/service-targets/{ACTIVE_ID}", headers=_headers()
        )
        assert repeated.status_code == 204
        assert len(events) == 2

    def test_exact_noop_revalidates_without_writing_and_reassign_preserves_creation(self, target_api):
        client, events, *_ = target_api
        original = _put(client).json()["target"]
        noop = _put(client)
        assert noop.status_code == 200
        assert noop.json()["target"] == original
        assert len(events) == 1

        changed = _put(client, session_id="session-b")
        assert changed.status_code == 200
        assert changed.json()["target"] == {
            **original,
            "session_id": "session-b",
            "updated_at": "2026-08-30T12:00:01.000Z",
        }
        assert len(events) == 2

    def test_paused_may_bind_but_cannot_be_served_and_released_is_terminal(self, target_api):
        client, events, *_ = target_api
        paused = _put(client, PAUSED_ID)
        assert paused.status_code == 201
        _error(_check(client, PAUSED_ID), 409, "reservation_paused")
        _error(_put(client, RELEASED_ID), 409, "reservation_released")
        assert len(events) == 1

    @pytest.mark.parametrize(
        ("reservation_id", "body", "code"),
        [
            ("NOT-A-UUID", {"session_id": "session-a", "port": 8000}, "invalid_reservation_id"),
            ("44444444-4444-4444-8444-444444444444", {"session_id": "session-a", "port": 8000}, "reservation_not_found"),
            (ACTIVE_ID, {"session_id": "bad session", "port": 8000}, "invalid_session_id"),
            (ACTIVE_ID, {"session_id": "session-a", "port": 0}, "invalid_port"),
            (ACTIVE_ID, {"session_id": "session-a", "port": True}, "invalid_port"),
            (ACTIVE_ID, {"session_id": "session-a", "port": 8000, "ip": "127.0.0.1"}, "unknown_fields"),
        ],
    )
    def test_request_refusals_are_exact(self, target_api, reservation_id, body, code):
        client, events, *_ = target_api
        response = client.put(
            f"/api/network/service-targets/{reservation_id}",
            json=body,
            headers=_headers(),
        )
        expected_status = 404 if code == "reservation_not_found" else 400
        _error(response, expected_status, code)
        assert events == []

    def test_cross_org_reservations_are_not_visible(self, target_api):
        client, events, *_ = target_api
        _error(
            client.put(
                f"/api/network/service-targets/{ACTIVE_ID}",
                json={"session_id": "session-a", "port": 8000},
                headers=_headers("other"),
            ),
            404,
            "reservation_not_found",
        )
        assert events == []

    def test_delete_rejects_a_body_without_removing_target(self, target_api):
        client, events, *_ = target_api
        _put(client)
        response = client.request(
            "DELETE",
            f"/api/network/service-targets/{ACTIVE_ID}",
            json={},
            headers=_headers(),
        )
        _error(response, 400, "unknown_fields")
        assert len(_target_members()) == 1
        assert len(events) == 1


class TestServiceTargetResolution:
    @pytest.mark.parametrize(
        ("session_id", "code"),
        [
            ("missing-session", "target_session_not_found"),
            ("other-session", "target_session_not_found"),
            # Host sessions are personal-scoped after the Compose cutover, so
            # an organization-scoped caller cannot discover their type.
            ("host-session", "target_session_not_found"),
            ("dead-session", "target_session_unavailable"),
        ],
    )
    def test_session_authority_and_liveness_fail_closed(self, target_api, session_id, code):
        client, events, *_ = target_api
        response = _put(client, session_id=session_id)
        status = 404 if code == "target_session_not_found" else 409
        _error(response, status, code)
        assert events == []

    def test_machine_network_and_tcp_are_required(self, target_api, monkeypatch):
        client, events, service, _containers, _reachable = target_api
        monkeypatch.setattr(service, "_read_local_machine_id", lambda: None)
        _error(_put(client), 503, "machine_identity_unavailable")

        monkeypatch.setattr(service, "_read_local_machine_id", lambda: MACHINE_ID)
        monkeypatch.setattr(service, "discover_topology", lambda: SimpleNamespace(network=""))
        _error(_put(client), 503, "compose_network_unavailable")

        monkeypatch.setattr(
            service, "discover_topology", lambda: SimpleNamespace(network="autonomy_default")
        )
        _error(_put(client, port=8001), 409, "target_port_unreachable")
        assert events == []

    def test_stale_container_and_machine_mismatch_refuse_serving(self, target_api):
        client, events, _service, containers, _reachable = target_api
        _put(client)
        assert len(events) == 1
        containers["session-a"] = _service.ContainerInspection(
            CONTAINER_A_REPLACED, "172.30.0.11"
        )
        _error(_check(client), 409, "target_stale")

        containers["session-a"] = _service.ContainerInspection(
            CONTAINER_A, "172.30.0.11"
        )
        member = _target_members()[0]
        settings_ops.upsert_by_key(
            TARGET_SET_ID,
            REVISION,
            ACTIVE_ID,
            {**member.payload, "machine_id": "ff" * 32},
            org="acme",
        )
        _error(_check(client), 409, "target_machine_mismatch")

    def test_no_docker_ip_or_caller_authority_is_stored_or_returned(self, target_api):
        client, _events, *_ = target_api
        response = _put(client)
        raw = _target_members()[0].payload
        private_or_authority = {"ip", "docker_ip", "network", "url", "upstream", "org"}
        assert (private_or_authority | {"reservation_id"}).isdisjoint(raw)
        assert private_or_authority.isdisjoint(response.json()["target"])
        assert "172.30.0.11" not in repr(raw)
        assert "172.30.0.11" not in response.text


def test_service_target_schema_is_organization_homed_and_keyed_by_reservation():
    from tools.graph import schemas
    from tools.graph.schemas import service_target

    schema = schemas.get_schema(TARGET_SET_ID, REVISION)
    assert schema is service_target.ServiceTargetV1
    assert schemas.declared_home(TARGET_SET_ID) == "organization"
    assert schemas.declared_band(TARGET_SET_ID, REVISION) == ("raw", "raw")
    assert schema._access_pattern == "keyed_per_entity"
    assert schema._key_strategy == "reservation_id"
    payload = {
        "machine_id": MACHINE_ID,
        "session_id": "session-a",
        "container_id": CONTAINER_A,
        "port": 8000,
        "created_at": "2026-08-30T12:00:00.000Z",
        "updated_at": "2026-08-30T12:00:00.000Z",
    }
    schemas.validate_payload(TARGET_SET_ID, REVISION, payload)
    schemas.validate_key(TARGET_SET_ID, REVISION, ACTIVE_ID)
    for forbidden in (
        "reservation_id",
        "organization",
        "org",
        "binding_state",
        "docker_ip",
        "network",
        "url",
        "upstream",
    ):
        with pytest.raises(schemas.SchemaValidationError):
            schemas.validate_payload(TARGET_SET_ID, REVISION, {**payload, forbidden: "x"})


def test_container_inspection_uses_an_argv_call_and_exact_compose_network(monkeypatch):
    from tools.dashboard import service_publication as service

    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "Id": CONTAINER_A,
                        "State": {"Running": True},
                        "NetworkSettings": {
                            "Networks": {
                                "autonomy_default": {"IPAddress": "172.30.0.11"}
                            }
                        },
                    }
                ]
            ),
        )

    monkeypatch.setattr(service.subprocess, "run", run)
    result = asyncio.run(
        service._inspect_session_container("session-a", "autonomy_default")
    )
    assert result == service.ContainerInspection(CONTAINER_A, "172.30.0.11")
    assert seen[0][0] == ["docker", "inspect", "session-a"]
    assert seen[0][1]["check"] is False


def test_container_inspection_refuses_a_container_outside_the_node_network(monkeypatch):
    from tools.dashboard import service_publication as service

    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "Id": CONTAINER_A,
                        "State": {"Running": True},
                        "NetworkSettings": {
                            "Networks": {"some_other_network": {"IPAddress": "172.31.0.4"}}
                        },
                    }
                ]
            ),
        ),
    )
    with pytest.raises(service.ServicePublicationError) as raised:
        asyncio.run(service._inspect_session_container("session-a", "autonomy_default"))
    assert (raised.value.code, raised.value.status_code) == (
        "target_network_unavailable",
        409,
    )


def test_host_network_container_uses_exact_compose_network_gateway(monkeypatch):
    from tools.dashboard import service_publication as service

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        if argv == ["docker", "inspect", "session-a"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [{
                        "Id": CONTAINER_A,
                        "State": {"Running": True},
                        "HostConfig": {"NetworkMode": "host"},
                        "NetworkSettings": {"Networks": {"host": {}}},
                    }]
                ),
            )
        assert argv == ["docker", "network", "inspect", "autonomy_default"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [{"IPAM": {"Config": [{"Gateway": "172.16.0.1"}]}}]
            ),
        )

    monkeypatch.setattr(service.subprocess, "run", run)
    result = asyncio.run(
        service._inspect_session_container("session-a", "autonomy_default")
    )
    assert result == service.ContainerInspection(CONTAINER_A, "172.16.0.1")
    assert seen == [
        ["docker", "inspect", "session-a"],
        ["docker", "network", "inspect", "autonomy_default"],
    ]


def test_host_network_container_derives_docker_gateway_when_ipam_omits_it(monkeypatch):
    from tools.dashboard import service_publication as service

    def run(argv, **kwargs):
        if argv == ["docker", "inspect", "session-a"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [{
                        "Id": CONTAINER_A,
                        "State": {"Running": True},
                        "HostConfig": {"NetworkMode": "host"},
                        "NetworkSettings": {"Networks": {"host": {}}},
                    }]
                ),
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [{"IPAM": {"Config": [{"Subnet": "172.16.0.0/24"}]}}]
            ),
        )

    monkeypatch.setattr(service.subprocess, "run", run)
    result = asyncio.run(
        service._inspect_session_container("session-a", "autonomy_default")
    )
    assert result == service.ContainerInspection(CONTAINER_A, "172.16.0.1")


@pytest.mark.parametrize("config", [[], [{"Gateway": "127.0.0.1"}]])
def test_host_network_container_refuses_unusable_compose_gateway(monkeypatch, config):
    from tools.dashboard import service_publication as service

    def run(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [{
                        "Id": CONTAINER_A,
                        "State": {"Running": True},
                        "HostConfig": {"NetworkMode": "host"},
                        "NetworkSettings": {"Networks": {"host": {}}},
                    }]
                ),
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"IPAM": {"Config": config}}]),
        )

    monkeypatch.setattr(service.subprocess, "run", run)
    with pytest.raises(service.ServicePublicationError) as raised:
        asyncio.run(service._inspect_session_container("session-a", "autonomy_default"))
    assert (raised.value.code, raised.value.status_code) == (
        "compose_network_unavailable",
        503,
    )
