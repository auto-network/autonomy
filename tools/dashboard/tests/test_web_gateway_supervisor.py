from __future__ import annotations

#: Target rows freeze a machine (ServiceTargetV1); the supervisor serves
#: only rows naming THIS one, so a stub needs both halves.
LOCAL_MACHINE = "aa" * 32

import asyncio
import contextlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from tools.dashboard import web_gateway_supervisor as sup
from tools.dashboard.service_gateway import ServiceGatewayRoute
from dataclasses import replace


class FakeRuntime:
    def __init__(self):
        self.running = False
        self.healthy = False
        self.starts = 0
        self.stops = 0
        self.marker = "not-started"
        self.helpers = ()

    async def reconcile_helpers(self, helpers, marker):
        self.helpers = helpers

    async def ensure_started(self):
        self.starts += 1
        self.running = True
        self.healthy = True
        self.marker = f"started-{self.starts}"

    async def is_healthy(self):
        return self.running and self.healthy

    async def instance_marker(self):
        return self.marker if self.running and self.healthy else None

    async def stop(self):
        self.stops += 1
        self.running = False
        self.healthy = False


class FakeLoader:
    def __init__(self):
        self.configs = []
        self.failure = None

    async def __call__(self, config):
        self.configs.append(config)
        if self.failure is not None:
            raise self.failure


def desired(*routes, config="config", ready=True, reason=None, detail=None):
    return sup.GatewayDesiredState(
        caddyfile=config,
        routes=tuple(sup.DesiredRoute(route_id, fingerprint) for route_id, fingerprint in routes),
        ready=ready,
        reason=reason,
        detail=detail,
    )


class NoopLeaseReconciler:
    async def reconcile(self):
        pass


@pytest.mark.asyncio
async def test_supervisor_applies_helper_change_even_when_caddy_route_is_unchanged():
    runtime, loader = FakeRuntime(), FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    first = sup.AuthHelper("org-oidc:anchore", "/run/auth/anchore", "v1")
    plan = replace(desired(("r1", "gate")), helpers=(first,))
    await supervisor.reconcile(plan)
    assert runtime.helpers == (first,)
    second = replace(first, revision="v2")
    await supervisor.reconcile(replace(plan, helpers=(second,)))
    assert runtime.helpers == (second,)
    assert len(loader.configs) == 2
    await supervisor.reconcile(replace(plan, helpers=()))
    assert runtime.helpers == ()


@pytest.fixture(autouse=True)
def materialized_service_certificate(monkeypatch):
    from tools.dashboard import service_auth
    monkeypatch.setattr(service_auth, "configuration", lambda org: {"configured": False, "default_access": "public"})
    monkeypatch.setattr(
        sup.service_certificate,
        "active_gateway_pair",
        lambda _org, _persona: (
            "/run/autonomy-service-gateway-certs/personas/test/tls.crt",
            "/run/autonomy-service-gateway-certs/personas/test/tls.key",
        ),
    )


@pytest.mark.asyncio
async def test_hostname_lease_reconciler_enrolls_once_and_replays_on_connector_restart():
    calls = []
    connector = ["connector-1"]

    def control(_org, op, args):
        calls.append((op, args))
        if op == "connector-status":
            return {
                "ok": True,
                "serving": True,
                "connector_instance": connector[0],
            }
        return {"ok": True}

    desired_leases = lambda: {"anchore": {"reservation-1": "app.example"}}
    reconciler = sup.HostnameLeaseReconciler(
        control_fn=control, desired_fn=desired_leases
    )

    await reconciler.reconcile()
    await reconciler.reconcile()
    connector[0] = "connector-2"
    await reconciler.reconcile()

    assert [call for call in calls if call[0] == "serve-host"] == [
        (
            "serve-host",
            {"reservation": "reservation-1", "host": "app.example"},
        ),
        (
            "serve-host",
            {"reservation": "reservation-1", "host": "app.example"},
        ),
    ]


@pytest.mark.asyncio
async def test_hostname_lease_reconciler_releases_removed_publication():
    calls = []
    desired = [{"anchore": {"reservation-1": "app.example"}}, {"anchore": {}}]

    def control(_org, op, args):
        calls.append((op, args))
        if op == "connector-status":
            return {
                "ok": True,
                "serving": True,
                "connector_instance": "connector-1",
            }
        return {"ok": True}

    reconciler = sup.HostnameLeaseReconciler(
        control_fn=control, desired_fn=lambda: desired.pop(0)
    )

    await reconciler.reconcile()
    await reconciler.reconcile()

    assert ("release-host", {"reservation": "reservation-1"}) in calls


@pytest.mark.asyncio
async def test_hostname_lease_reconciler_retries_refused_enrollment():
    attempts = []

    def control(_org, op, args):
        if op == "connector-status":
            return {
                "ok": True,
                "serving": True,
                "connector_instance": "connector-1",
            }
        attempts.append(args)
        return {"ok": len(attempts) > 1}

    reconciler = sup.HostnameLeaseReconciler(
        control_fn=control,
        desired_fn=lambda: {"anchore": {"reservation-1": "app.example"}},
    )

    await reconciler.reconcile()
    await reconciler.reconcile()

    assert len(attempts) == 2


def test_desired_hostname_leases_come_only_from_targeted_live_settings(monkeypatch):
    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["anchore"])
    monkeypatch.setattr(
        sup.service_publication,
        "list_reservations",
        lambda _org: [
            {"reservation_id": "active", "state": "active"},
            {"reservation_id": "paused", "state": "paused"},
            {"reservation_id": "released", "state": "released"},
            {"reservation_id": "untargeted", "state": "active"},
        ],
    )
    monkeypatch.setattr(
        sup.service_publication, "_read_local_machine_id",
        lambda: LOCAL_MACHINE,
    )
    monkeypatch.setattr(
        sup.service_publication,
        "list_service_targets",
        lambda _org: [
            {"reservation_id": "active", "machine_id": LOCAL_MACHINE},
            {"reservation_id": "paused", "machine_id": LOCAL_MACHINE},
            {"reservation_id": "released", "machine_id": LOCAL_MACHINE},
        ],
    )
    monkeypatch.setattr(
        sup.service_gateway,
        "reservation_hostname",
        lambda org, reservation: f"{reservation}.{org}.example",
    )

    assert sup._desired_hostname_leases() == {
        "anchore": {
            "active": "active.anchore.example",
            "paused": "paused.anchore.example",
        }
    }


@pytest.mark.asyncio
async def test_worker_reconciles_hostname_before_certificate_gated_plan():
    order = []

    class LeaseReconciler:
        async def reconcile(self):
            order.append("leases")

    class Supervisor:
        async def reconcile(self, plan, force=False):
            order.append(("gateway", plan.reason))
            return {"state": "stopped"}

    async def planner():
        order.append("plan")
        return desired(ready=False, reason="certificate-unavailable")

    worker = sup.GatewayReconcileWorker(
        supervisor=Supervisor(),
        planner=planner,
        lease_reconciler=LeaseReconciler(),
    )

    await worker.reconcile_once()

    assert order == [
        "leases",
        "plan",
        ("gateway", "certificate-unavailable"),
    ]


@pytest.mark.asyncio
async def test_lease_failure_does_not_skip_gateway_fail_closed_plan():
    observed = []

    class LeaseReconciler:
        async def reconcile(self):
            raise RuntimeError("settings unavailable")

    class Supervisor:
        async def reconcile(self, plan, force=False):
            observed.append(plan.reason)
            return {"state": "stopped"}

    async def planner():
        return desired(ready=False, reason="authority-unavailable")

    worker = sup.GatewayReconcileWorker(
        supervisor=Supervisor(),
        planner=planner,
        lease_reconciler=LeaseReconciler(),
    )

    await worker.reconcile_once()

    assert observed == ["authority-unavailable"]


def test_gateway_org_discovery_serves_shared_orgs_and_the_personal_scope(monkeypatch):
    """Personal is the default publisher of the dashboard's relay route
    (auto-4urxx); followed mirrors publish nothing."""
    from tools.dashboard import link_serving_supervisor
    from tools.graph import org_ops

    monkeypatch.setattr(
        link_serving_supervisor,
        "_discover_startup_orgs",
        lambda: [None, "personal", "autonomy"],
    )
    monkeypatch.setattr(
        org_ops,
        "list_orgs",
        lambda: [
            SimpleNamespace(slug="personal", type="personal"),
            SimpleNamespace(slug="autonomy", type="shared"),
            SimpleNamespace(slug="mirror", type="followed"),
        ],
    )

    assert sup._discover_orgs() == ["autonomy", "personal"]


@pytest.mark.asyncio
async def test_empty_desired_state_keeps_gateway_dormant():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)

    result = await supervisor.reconcile(desired())

    assert result["state"] == "stopped"
    assert result["advertised_routes"] == []
    assert runtime.starts == 0
    assert loader.configs == []


@pytest.mark.asyncio
async def test_route_is_advertised_only_after_healthy_atomic_load():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)

    result = await supervisor.reconcile(desired(("r1", "route-v1"), config="full-v1"))

    assert runtime.starts == 1
    assert loader.configs == ["full-v1"]
    assert result["state"] == "healthy"
    assert result["advertised_routes"] == ["r1"]
    assert result["config_revision"] == 1


@pytest.mark.asyncio
async def test_unchanged_full_state_is_a_true_noop():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    plan = desired(("r1", "route-v1"), config="full-v1")
    await supervisor.reconcile(plan)

    result = await supervisor.reconcile(plan)

    assert runtime.starts == 1
    assert loader.configs == ["full-v1"]
    assert result["state"] == "healthy"
    assert result["config_revision"] == 1


@pytest.mark.asyncio
async def test_failed_add_keeps_only_unchanged_last_known_good_routes():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    await supervisor.reconcile(desired(("r1", "route-v1"), config="full-v1"))
    loader.failure = RuntimeError("adapt failed")

    result = await supervisor.reconcile(
        desired(("r1", "route-v1"), ("r2", "route-v1"), config="full-v2")
    )

    assert result["state"] == "failed"
    assert result["advertised_routes"] == ["r1"]
    assert runtime.running is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_plan", "next_plan"),
    [
        (
            desired(("r1", "route-v1"), ("r2", "route-v1"), config="full-v1"),
            desired(("r2", "route-v1"), config="removed-r1"),
        ),
        (
            desired(("r1", "route-v1"), config="full-v1"),
            desired(("r1", "route-v2"), config="retargeted"),
        ),
    ],
)
async def test_failed_remove_or_retarget_stops_stale_authority(initial_plan, next_plan):
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    await supervisor.reconcile(initial_plan)
    loader.failure = RuntimeError("adapt failed")

    result = await supervisor.reconcile(next_plan)

    assert result["state"] == "failed"
    assert result["advertised_routes"] == []
    assert runtime.running is False
    assert runtime.stops == 1


@pytest.mark.asyncio
async def test_final_route_removal_stops_without_loading_an_empty_config():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    await supervisor.reconcile(desired(("r1", "route-v1"), config="full-v1"))

    result = await supervisor.reconcile(desired(config="empty"))

    assert result["state"] == "stopped"
    assert result["advertised_routes"] == []
    assert runtime.running is False
    assert runtime.stops == 1
    assert loader.configs == ["full-v1"]


@pytest.mark.asyncio
async def test_unready_dependency_stops_and_never_advertises():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    await supervisor.reconcile(desired(("r1", "route-v1")))

    result = await supervisor.reconcile(
        desired(("r1", "route-v1"), ready=False, reason="connector-unavailable",
                detail="personal: running=False reason=no-live-grants")
    )

    assert result["state"] == "stopped"
    assert result["reason"] == "connector-unavailable"
    assert result["detail"] == "personal: running=False reason=no-live-grants"
    assert result["advertised_routes"] == []
    assert runtime.stops == 1
    # A ready plan clears the detail with the reason.
    result = await supervisor.reconcile(desired(("r1", "route-v1")))
    assert "detail" not in result


@pytest.mark.asyncio
async def test_dead_gateway_is_restarted_and_complete_state_reloaded():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    plan = desired(("r1", "route-v1"), config="full-v1")
    await supervisor.reconcile(plan)
    runtime.running = False
    runtime.healthy = False

    result = await supervisor.reconcile(plan)

    assert runtime.starts == 2
    assert loader.configs == ["full-v1", "full-v1"]
    assert result["state"] == "healthy"
    assert result["advertised_routes"] == ["r1"]


@pytest.mark.asyncio
async def test_same_container_process_restart_forces_complete_reload():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    plan = desired(("r1", "route-v1"), config="full-v1")
    await supervisor.reconcile(plan)

    # Docker's unless-stopped policy can restart PID 1 in the same container;
    # health recovers, but Caddy has only bootstrap.json until we reload.
    runtime.marker = "externally-restarted"
    result = await supervisor.reconcile(plan)

    assert loader.configs == ["full-v1", "full-v1"]
    assert result["state"] == "healthy"
    assert result["advertised_routes"] == ["r1"]
    assert result["config_revision"] == 2


@pytest.mark.asyncio
async def test_identical_failure_backs_off_but_changed_authority_reconciles_now():
    runtime = FakeRuntime()
    loader = FakeLoader()
    loader.failure = RuntimeError("adapt failed")
    clock = [100.0]
    supervisor = sup.WebGatewaySupervisor(
        runtime=runtime, loader=loader, now=lambda: clock[0]
    )
    broken = desired(("r1", "route-v1"), config="broken-v1")

    first = await supervisor.reconcile(broken)
    immediate = await supervisor.reconcile(broken)
    changed = await supervisor.reconcile(
        desired(("r1", "route-v2"), config="broken-v2")
    )

    assert first["state"] == "failed"
    assert immediate["state"] == "backoff"
    assert immediate["reason"] == "backoff"
    assert len(loader.configs) == 2
    assert changed["state"] == "failed"

    clock[0] += sup.INITIAL_BACKOFF_SECONDS
    await supervisor.reconcile(desired(("r1", "route-v2"), config="broken-v2"))
    assert len(loader.configs) == 3


@pytest.mark.asyncio
async def test_planner_builds_complete_active_and_paused_config(monkeypatch):
    active_id = "91674161-2d14-55a0-be9d-21237d02c2dc"
    paused_id = "d1fbc17d-527d-5c3c-b276-37e62963a693"
    released_id = "ccb27ab1-0a3a-598d-bda1-dd3637d70dc3"
    active_host = "app.persona-77827e972ba4c37d4215.serve.auto.network"
    paused_host = "paused.persona-77827e972ba4c37d4215.serve.auto.network"
    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["autonomy"])

    async def connector_ready(org):
        return org == "autonomy"

    monkeypatch.setattr(sup, "_connector_ready", connector_ready)
    monkeypatch.setattr(
        sup.service_publication,
        "list_reservations",
        lambda org: [
            {"reservation_id": active_id, "state": "active", "persona_label": "persona-test"},
            {"reservation_id": paused_id, "state": "paused", "persona_label": "persona-test"},
            {"reservation_id": released_id, "state": "released", "persona_label": "persona-test"},
        ],
    )
    monkeypatch.setattr(
        sup.service_publication, "_read_local_machine_id",
        lambda: LOCAL_MACHINE,
    )
    monkeypatch.setattr(
        sup.service_publication,
        "list_service_targets",
        lambda org: [
            {"reservation_id": active_id, "machine_id": LOCAL_MACHINE},
            {"reservation_id": paused_id, "machine_id": LOCAL_MACHINE},
            {"reservation_id": released_id, "machine_id": LOCAL_MACHINE},
        ],
    )

    async def resolve(org, reservation_id):
        assert reservation_id == active_id
        return ServiceGatewayRoute(
            reservation_id=active_id,
            hostname=active_host,
            session_id="auto-0831-171125",
            container_id="a" * 64,
            network="autonomy_default",
            upstream_ip="172.16.0.42",
            port=8000,
            expires_at="2026-08-31T21:12:00.000Z",
        )

    monkeypatch.setattr(sup.service_gateway, "resolve_gateway_route", resolve)
    monkeypatch.setattr(
        sup.service_gateway,
        "reservation_hostname",
        lambda org, reservation_id: paused_host,
    )

    plan = await sup.build_desired_state()

    assert plan.ready is True
    assert [route.route_id for route in plan.routes] == [active_id, paused_id]
    assert f"reverse_proxy 172.16.0.42:8000" in plan.caddyfile
    assert f"https://{paused_host}:9443" in plan.caddyfile
    assert "This service is paused" in plan.caddyfile
    assert "owner has paused this publication" in plan.caddyfile
    assert released_id not in plan.caddyfile


@pytest.mark.asyncio
async def test_planner_stays_dormant_without_certificate(monkeypatch):
    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["autonomy"])
    monkeypatch.setattr(
        sup.service_publication,
        "list_reservations",
        lambda _org: [
            {"reservation_id": "r1", "state": "active", "persona_label": "persona-test"}
        ],
    )
    monkeypatch.setattr(
        sup.service_publication, "_read_local_machine_id",
        lambda: LOCAL_MACHINE,
    )
    monkeypatch.setattr(
        sup.service_publication,
        "list_service_targets",
        lambda _org: [{"reservation_id": "r1", "machine_id": LOCAL_MACHINE}],
    )
    monkeypatch.setattr(
        sup.service_certificate, "active_gateway_pair", lambda _org, _persona: None
    )

    async def connector_ready(_org):
        return True

    monkeypatch.setattr(sup, "_connector_ready", connector_ready)

    plan = await sup.build_desired_state()

    assert plan.routes == ()
    assert plan.ready is False
    assert plan.reason == "certificate-unavailable"


@pytest.mark.asyncio
async def test_planner_stays_dormant_until_connector_is_serving(monkeypatch):
    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["autonomy"])
    monkeypatch.setattr(
        sup.service_publication,
        "list_reservations",
        lambda org: [{"reservation_id": "r1", "state": "active", "persona_label": "persona-test"}],
    )
    monkeypatch.setattr(
        sup.service_publication, "_read_local_machine_id",
        lambda: LOCAL_MACHINE,
    )
    monkeypatch.setattr(
        sup.service_publication,
        "list_service_targets",
        lambda org: [{"reservation_id": "r1", "machine_id": LOCAL_MACHINE}],
    )

    async def connector_not_ready(_org):
        return False

    monkeypatch.setattr(sup, "_connector_ready", connector_not_ready)
    monkeypatch.setattr(sup, "_connector_outcome",
                        lambda org: f"{org}: running=False reason=no-live-grants")

    plan = await sup.build_desired_state()

    assert plan.routes == ()
    assert plan.ready is False
    assert plan.reason == "connector-unavailable"
    # The supervisor's own refusal travels with the reason (Windows run 5).
    assert plan.detail == "autonomy: running=False reason=no-live-grants"


@pytest.mark.asyncio
async def test_planner_replaces_stale_active_target_with_unavailable_route(monkeypatch):
    hostname = "stale.persona-77827e972ba4c37d4215.serve.auto.network"
    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["autonomy"])
    monkeypatch.setattr(
        sup.service_publication,
        "list_reservations",
        lambda org: [{"reservation_id": "r1", "state": "active", "persona_label": "persona-test"}],
    )
    monkeypatch.setattr(
        sup.service_publication, "_read_local_machine_id",
        lambda: LOCAL_MACHINE,
    )
    monkeypatch.setattr(
        sup.service_publication,
        "list_service_targets",
        lambda org: [{"reservation_id": "r1", "machine_id": LOCAL_MACHINE}],
    )

    async def connector_ready(_org):
        return True

    async def stale_target(_org, _reservation_id):
        raise RuntimeError("session exited")

    monkeypatch.setattr(sup, "_connector_ready", connector_ready)
    monkeypatch.setattr(sup.service_gateway, "resolve_gateway_route", stale_target)
    monkeypatch.setattr(
        sup.service_gateway,
        "reservation_hostname",
        lambda _org, _reservation_id: hostname,
    )

    plan = await sup.build_desired_state()

    assert [route.route_id for route in plan.routes] == ["r1"]
    assert f"https://{hostname}:9443" in plan.caddyfile
    assert "This service is not running" in plan.caddyfile
    assert "publication still exists" in plan.caddyfile
    assert " 503" in plan.caddyfile


@pytest.mark.asyncio
async def test_planner_keeps_healthy_sibling_when_another_target_is_unavailable(monkeypatch):
    healthy_host = "healthy.persona-77827e972ba4c37d4215.serve.auto.network"
    unavailable_host = "down.persona-77827e972ba4c37d4215.serve.auto.network"
    down_id = "7629c755-5c91-5a64-9dd8-5f171920291f"
    healthy_id = "efde8d86-e51c-558e-a560-3f118b65081c"
    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["autonomy"])
    monkeypatch.setattr(
        sup.service_publication,
        "list_reservations",
        lambda _org: [
            {
                "reservation_id": healthy_id,
                "state": "active",
                "persona_label": "persona-test",
            },
            {
                "reservation_id": down_id,
                "state": "active",
                "persona_label": "persona-test",
            },
        ],
    )
    monkeypatch.setattr(
        sup.service_publication, "_read_local_machine_id",
        lambda: LOCAL_MACHINE,
    )
    monkeypatch.setattr(
        sup.service_publication,
        "list_service_targets",
        lambda _org: [{"reservation_id": healthy_id, "machine_id": LOCAL_MACHINE}, {"reservation_id": down_id, "machine_id": LOCAL_MACHINE}],
    )

    async def connector_ready(_org):
        return True

    async def resolve(_org, reservation_id):
        if reservation_id == down_id:
            raise RuntimeError("session exited")
        return ServiceGatewayRoute(
            reservation_id=healthy_id,
            hostname=healthy_host,
            session_id="auto-0831-171125",
            container_id="a" * 64,
            network="autonomy_default",
            upstream_ip="172.16.0.42",
            port=8000,
            expires_at="2026-09-02T22:00:00.000Z",
        )

    monkeypatch.setattr(sup, "_connector_ready", connector_ready)
    monkeypatch.setattr(sup.service_gateway, "resolve_gateway_route", resolve)
    monkeypatch.setattr(
        sup.service_gateway,
        "reservation_hostname",
        lambda _org, reservation_id: (
            unavailable_host if reservation_id == down_id else healthy_host
        ),
    )

    plan = await sup.build_desired_state()

    assert plan.ready is True
    assert [route.route_id for route in plan.routes] == [down_id, healthy_id]
    assert f"reverse_proxy 172.16.0.42:8000" in plan.caddyfile
    assert f"https://{unavailable_host}:9443" in plan.caddyfile
    assert "This service is not running" in plan.caddyfile


@pytest.mark.asyncio
async def test_planner_failure_refuses_to_preserve_unverified_authority(monkeypatch):
    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["autonomy"])

    def unreadable(_org):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(sup.service_publication, "list_reservations", unreadable)

    plan = await sup.build_desired_state()

    assert plan.routes == ()
    assert plan.ready is False
    assert plan.reason == "authority-unavailable"


@pytest.mark.asyncio
async def test_compose_runtime_starts_checks_health_and_stops_exact_service(
    monkeypatch,
):
    calls = []
    container_id = "b" * 64

    async def runner(argv, timeout):
        calls.append((argv, timeout))
        if argv[-3:] == ["ps", "-q", "service-gateway"]:
            return subprocess.CompletedProcess(argv, 0, container_id + "\n", "")
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [
                        {
                            "State": {
                                "Running": True,
                                "Health": {"Status": "healthy"},
                            }
                        }
                    ]
                ),
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    # This is a daemon-side host path. It must never become Compose's
    # client-side project directory inside the Dashboard container.
    monkeypatch.setenv("AUTONOMY_HOST_ROOT", "/opt/autonomy/code")
    runtime = sup.ComposeGatewayRuntime(runner=runner, sleep=lambda _delay: None)

    await runtime.ensure_started()
    assert await runtime.is_healthy() is True
    await runtime.stop()

    command_text = [" ".join(call[0]) for call in calls]
    assert any(
        text.endswith("up -d --no-deps --no-build service-gateway")
        for text in command_text
    )
    assert any(text.endswith("rm -f -s service-gateway") for text in command_text)
    compose_commands = [text for text in command_text if text.startswith("docker compose ")]
    assert all("--project-directory /app" in text for text in compose_commands)
    assert all(
        "--project-directory /opt/autonomy/code" not in text
        for text in compose_commands
    )
    assert all("-f /app/docker-compose.yml" in text for text in compose_commands)
    assert all("autonomy-trial" not in text for text in command_text)


@pytest.mark.asyncio
async def test_compose_runtime_treats_missing_docker_as_not_running():
    async def missing_docker(_argv, _timeout):
        raise FileNotFoundError("docker")

    runtime = sup.ComposeGatewayRuntime(runner=missing_docker)

    assert await runtime.is_healthy() is False


@pytest.mark.asyncio
async def test_worker_reconciles_startup_and_relevant_events_only():
    plans = [desired(), desired(("r1", "route-v1"), config="full-v1")]
    observed = []

    class Supervisor:
        async def reconcile(self, plan, force=False):
            observed.append(plan)
            return {"state": "healthy"}

        def status(self):
            return {"state": "healthy"}

    async def planner():
        return plans.pop(0)

    class EventBus:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.unsubscribed = False

        def subscribe(self, **_kwargs):
            return self.queue

        def unsubscribe(self, queue):
            assert queue is self.queue
            self.unsubscribed = True

    bus = EventBus()
    worker = sup.GatewayReconcileWorker(
        supervisor=Supervisor(),
        planner=planner,
        lease_reconciler=NoopLeaseReconciler(),
    )

    assert worker.event_relevant(
        "setting.changed", {"set_id": sup.NAMESPACE_RESERVATION_SET_ID}
    )
    assert worker.event_relevant("session:registry", {})
    assert not worker.event_relevant("nav", {})
    await worker.start(bus)
    for _ in range(100):
        if len(observed) == 1:
            break
        await asyncio.sleep(0)
    await bus.queue.put(("nav", {}, 1))
    await bus.queue.put(("session:registry", {}, 2))
    for _ in range(100):
        if len(observed) == 2:
            break
        await asyncio.sleep(0)
    await worker.stop()

    assert len(observed) == 2
    assert bus.unsubscribed is True


@pytest.mark.asyncio
async def test_worker_watchdog_is_not_starved_by_unrelated_events(monkeypatch):
    """A busy EventBus must not turn the watchdog into an idle timer."""
    monkeypatch.setattr(sup, "RECONCILE_INTERVAL_SECONDS", 0.01)
    observed = []

    class Supervisor:
        async def reconcile(self, plan, force=False):
            observed.append(plan)
            return {"state": "stopped"}

    async def planner():
        return desired()

    class EventBus:
        def __init__(self):
            self.queue = asyncio.Queue()

        def subscribe(self, **_kwargs):
            return self.queue

        def unsubscribe(self, _queue):
            pass

    bus = EventBus()
    worker = sup.GatewayReconcileWorker(
        supervisor=Supervisor(),
        planner=planner,
        lease_reconciler=NoopLeaseReconciler(),
    )

    async def flood_unrelated_events():
        while True:
            await bus.queue.put(("nav", {}, 1))
            await asyncio.sleep(0.001)

    await worker.start(bus)
    flood = asyncio.create_task(flood_unrelated_events())
    try:
        await asyncio.sleep(0.04)
    finally:
        flood.cancel()
        await worker.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await flood

    assert len(observed) >= 2


@pytest.mark.asyncio
async def test_restart_failure_clears_routes_from_runtime_status():
    runtime = FakeRuntime()
    runtime.running = True
    runtime.healthy = True
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=FakeLoader())
    await supervisor.reconcile(desired(("r1", "route-v1"), config="full-v1"))
    runtime.healthy = False

    async def fail_start():
        raise RuntimeError("compose start failed")

    runtime.ensure_started = fail_start
    status = await supervisor.reconcile(
        desired(("r1", "route-v1"), config="full-v1")
    )

    assert status["state"] == "failed"
    assert status["advertised_routes"] == []
    assert "compose start failed" in status["error"]


@pytest.mark.asyncio
async def test_confirmed_dormant_state_does_not_poll_docker_each_tick():
    class CountingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.health_checks = 0

        async def is_healthy(self):
            self.health_checks += 1
            return await super().is_healthy()

    runtime = CountingRuntime()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=FakeLoader())

    await supervisor.reconcile(desired())
    await supervisor.reconcile(desired())

    assert runtime.health_checks == 1


# ── auto-nkxko: cadence and off-loop discovery ─────────────────────────────

def test_watchdog_interval_is_no_longer_one_second():
    """Event-driven reconciles do the real work; the heartbeat only bounds a
    missed event. At 1 s it made org discovery the loop's busiest work."""
    assert sup.RECONCILE_INTERVAL_SECONDS >= 30.0


@pytest.mark.asyncio
async def test_planner_discovers_orgs_off_the_event_loop(monkeypatch):
    import threading
    seen = []

    def discover():
        seen.append(threading.current_thread() is threading.main_thread())
        return []
    monkeypatch.setattr(sup, "_discover_orgs", discover)
    state = await sup._build_desired_state()
    assert seen == [False], "org discovery must run in a worker thread"
    assert state is not None


# ── auto-nh1po: the reconciler declares this machine as the serving machine


SERVING_MACHINE = "ef" * 32


@pytest.mark.asyncio
async def test_hostname_lease_reconciler_declares_the_connectors_machine():
    calls = []

    def control(_org, op, args):
        calls.append((op, args))
        if op == "connector-status":
            return {
                "ok": True, "serving": True, "connector_instance": "c-1",
                "serving_slot": {"persona_pub": "ab" * 32, "machine": SERVING_MACHINE},
            }
        return {"ok": True}

    reconciler = sup.HostnameLeaseReconciler(
        control_fn=control,
        desired_fn=lambda: {"anchore": {"reservation-1": "app.example"}},
    )
    await reconciler.reconcile()

    assert [c for c in calls if c[0] == "serve-host"] == [(
        "serve-host",
        {"reservation": "reservation-1", "host": "app.example",
         "machine": SERVING_MACHINE},
    )]


@pytest.mark.asyncio
async def test_hostname_lease_reconciler_ignores_a_malformed_slot_machine():
    calls = []

    def control(_org, op, args):
        calls.append((op, args))
        if op == "connector-status":
            return {
                "ok": True, "serving": True, "connector_instance": "c-1",
                "serving_slot": {"persona_pub": "ab" * 32, "machine": None},
            }
        return {"ok": True}

    reconciler = sup.HostnameLeaseReconciler(
        control_fn=control,
        desired_fn=lambda: {"anchore": {"reservation-1": "app.example"}},
    )
    await reconciler.reconcile()

    assert [c for c in calls if c[0] == "serve-host"] == [(
        "serve-host", {"reservation": "reservation-1", "host": "app.example"},
    )]


@pytest.mark.asyncio
async def test_hostname_lease_reconciler_releases_a_stale_pin_then_declares():
    """The service moved here and the previous machine did not release
    (dead): host-owned-elsewhere -> release-host -> serve-host, once."""
    calls = []
    registers = []

    def control(_org, op, args):
        calls.append((op, args))
        if op == "connector-status":
            return {
                "ok": True, "serving": True, "connector_instance": "c-1",
                "serving_slot": {"persona_pub": "ab" * 32, "machine": SERVING_MACHINE},
            }
        if op == "serve-host":
            registers.append(args)
            if len(registers) == 1:
                return {"ok": False, "error": "host-owned-elsewhere"}
            return {"ok": True}
        return {"ok": True}

    reconciler = sup.HostnameLeaseReconciler(
        control_fn=control,
        desired_fn=lambda: {"anchore": {"reservation-1": "app.example"}},
    )
    await reconciler.reconcile()
    await reconciler.reconcile()  # applied: no repeat

    assert [c[0] for c in calls] == [
        "connector-status", "serve-host", "release-host", "serve-host",
        "connector-status",
    ]
    assert ("release-host", {"reservation": "reservation-1"}) in calls


@pytest.mark.asyncio
async def test_hostname_lease_reconciler_does_not_release_a_live_pin():
    """release-host is refused (lease-held) while the other machine serves:
    the reconciler stops there and retries next tick, never displacing."""
    calls = []

    def control(_org, op, args):
        calls.append((op, args))
        if op == "connector-status":
            return {
                "ok": True, "serving": True, "connector_instance": "c-1",
                "serving_slot": {"persona_pub": "ab" * 32, "machine": SERVING_MACHINE},
            }
        if op == "serve-host":
            return {"ok": False, "error": "host-owned-elsewhere"}
        return {"ok": False, "error": "lease-held"}

    reconciler = sup.HostnameLeaseReconciler(
        control_fn=control,
        desired_fn=lambda: {"anchore": {"reservation-1": "app.example"}},
    )
    await reconciler.reconcile()
    await reconciler.reconcile()

    assert [c[0] for c in calls] == [
        "connector-status", "serve-host", "release-host",
        "connector-status", "serve-host", "release-host",
    ]


def test_runtime_import_survives_an_unreadable_helper_override(tmp_path, caplog):
    """The runtime is built at import. On a box where /run/autonomy-keycache is
    root-only (dev, CI, a test runner), the helper record must read as absent,
    not raise and stop the dashboard from loading."""
    import os
    import stat

    locked = tmp_path / "service-auth"
    locked.mkdir()
    override = locked / "compose.json"
    override.write_text('{"services": {}}')
    locked.chmod(0)
    try:
        if os.access(override, os.R_OK):
            pytest.skip("running as a user that ignores directory modes")
        runtime = sup.ComposeGatewayRuntime(helper_override=str(override))
        assert runtime._helpers == ()
        assert runtime.helpers_known is False
        assert "unreadable" in caplog.text
        status = sup.WebGatewaySupervisor(runtime=runtime, loader=lambda: None).status()
        assert status["managed_helpers"] == "unknown"
    finally:
        locked.chmod(stat.S_IRWXU)

    missing = sup.ComposeGatewayRuntime(helper_override=str(tmp_path / "absent.json"))
    assert missing._helpers == ()
    assert missing.helpers_known is True
    assert "managed_helpers" not in sup.WebGatewaySupervisor(runtime=missing, loader=lambda: None).status()


@pytest.mark.asyncio
async def test_planner_gates_the_personal_dashboard_route_with_the_passkey_helper(monkeypatch, tmp_path):
    """Operator decision 2026-09-27: the personal route is gated by the
    dashboard's own passkey helper. The planner materializes it once, gives
    its loopback port to the gate, leaves the enrollment path unlogged, and
    the helper's own compose service travels with the AuthHelper."""
    from tools.dashboard import passkey_gate

    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["personal"])
    from tools.dashboard import service_auth
    monkeypatch.setattr(service_auth, "configuration", lambda org: {"configured": False})
    dash = "5030b922-d6cc-565c-8209-f675fa755226"
    monkeypatch.setattr(
        sup.service_publication, "list_reservations",
        lambda _org: [{"reservation_id": dash, "state": "active", "persona_label": "alice-x"}],
    )
    monkeypatch.setattr(sup.service_publication, "_read_local_machine_id", lambda: LOCAL_MACHINE)
    monkeypatch.setattr(
        sup.service_publication, "list_service_targets",
        lambda _org: [{"reservation_id": dash, "machine_id": LOCAL_MACHINE, "kind": "dashboard",
                       "access_mode": "personal"}],
    )
    monkeypatch.setattr(sup.service_certificate, "active_gateway_pair", lambda _org, _persona: (
        "/run/autonomy-service-gateway-certs/personas/alice-x/tls.crt",
        "/run/autonomy-service-gateway-certs/personas/alice-x/tls.key"))

    async def connector_ready(_org):
        return True

    monkeypatch.setattr(sup, "_connector_ready", connector_ready)
    hostname = "dashboard.alice-x.serve.auto.network"

    async def resolve(org, reservation_id):
        return ServiceGatewayRoute(
            reservation_id=dash, hostname=hostname, session_id="dashboard", container_id="a" * 64,
            network="autonomy_default", upstream_ip="172.16.0.9", port=8081,
            expires_at="2026-08-31T21:12:00.000Z",
        )

    monkeypatch.setattr(sup.service_gateway, "resolve_gateway_route", resolve)
    materialized = []

    def materialize(host, port, upstream):
        materialized.append((host, port, upstream))
        return sup.AuthHelper("dashboard-passkey", str(tmp_path), "rev-1", service={"image": "node", "volumes": [
            {"type": "bind", "source": str(tmp_path), "target": "/run/gate", "read_only": True}]})

    monkeypatch.setattr(passkey_gate, "materialize_helper", materialize)

    plan = await sup.build_desired_state()

    assert plan.ready is True, plan
    assert materialized == [(hostname, sup.helper_listener_ports(["dashboard-passkey"])["dashboard-passkey"], "172.16.0.9:8081")]
    assert [h.helper_id for h in plan.helpers] == ["dashboard-passkey"]
    assert plan.helpers[0].service["image"] == "node"
    assert "forward_auth 127.0.0.1:" in plan.caddyfile and "uri /oauth2/auth" in plan.caddyfile
    assert 'log_skip "/oauth2/enroll"' in plan.caddyfile
    assert "reverse_proxy 172.16.0.9:8081" in plan.caddyfile


def test_helper_reconciliation_uses_the_helpers_own_service_and_recovers_its_runtime_dir(tmp_path):
    """A helper that renders its own compose service (the passkey helper)
    is written as-is, and read back with the right runtime directory."""
    override = tmp_path / "compose.json"
    runtime = sup.ComposeGatewayRuntime(helper_override=str(override))
    passkey = sup.AuthHelper("dashboard-passkey", str(tmp_path / "gate"), "rev-1", service={
        "image": "node", "labels": {"autonomy.auth-config": "rev-1"},
        "volumes": [{"type": "bind", "source": str(tmp_path / "gate"), "target": "/run/gate", "read_only": True}]})
    oidc = sup.AuthHelper("org-oidc:acme", str(tmp_path / "org-oidc-acme"), "rev-2")
    from pathlib import Path

    from tools.network.service_auth import render_helper_service
    services = {passkey.service_name: passkey.service,
                oidc.service_name: render_helper_service(Path(oidc.runtime_dir), oidc.revision)}
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({"services": services}))
    again = sup.ComposeGatewayRuntime(helper_override=str(override))
    by_id = {h.helper_id: h for h in again._helpers}
    assert by_id["dashboard-passkey"].runtime_dir == str(tmp_path / "gate")
    assert by_id["dashboard-passkey"].service["image"] == "node"
    assert by_id["org-oidc:acme"].runtime_dir == str(tmp_path / "org-oidc-acme")
    assert runtime is not None


@pytest.mark.asyncio
async def test_planner_renders_a_personal_session_service_unavailable_not_gated_under_another_name(monkeypatch, tmp_path):
    """The passkey gate is bound to the dashboard route's hostname (the
    relying party); a session Service under the personal mode has no gate
    of its own name yet and renders the unavailable page, never a gate for
    another hostname."""
    from tools.dashboard import passkey_gate, service_auth

    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["personal"])
    monkeypatch.setattr(service_auth, "configuration", lambda org: {"configured": False})
    rid = "6b5ef2c1-1b41-5f7a-9c7d-2f0b6a3d4e55"
    host = "docs.alice-x.serve.auto.network"
    monkeypatch.setattr(sup.service_publication, "list_reservations",
                        lambda _org: [{"reservation_id": rid, "state": "active", "persona_label": "alice-x"}])
    monkeypatch.setattr(sup.service_publication, "_read_local_machine_id", lambda: LOCAL_MACHINE)
    monkeypatch.setattr(sup.service_publication, "list_service_targets",
                        lambda _org: [{"reservation_id": rid, "machine_id": LOCAL_MACHINE, "access_mode": "personal"}])
    monkeypatch.setattr(sup.service_certificate, "active_gateway_pair", lambda _org, _persona: (
        "/run/autonomy-service-gateway-certs/personas/alice-x/tls.crt",
        "/run/autonomy-service-gateway-certs/personas/alice-x/tls.key"))

    async def connector_ready(_org):
        return True

    monkeypatch.setattr(sup, "_connector_ready", connector_ready)

    async def resolve(org, reservation_id):
        return ServiceGatewayRoute(reservation_id=rid, hostname=host, session_id="auto-0831-171125",
                                   container_id="a" * 64, network="autonomy_default", upstream_ip="172.16.0.42",
                                   port=8000, expires_at="2026-08-31T21:12:00.000Z")

    monkeypatch.setattr(sup.service_gateway, "resolve_gateway_route", resolve)
    monkeypatch.setattr(sup.service_gateway, "reservation_hostname", lambda org, reservation_id: host)
    monkeypatch.setattr(passkey_gate, "materialize_helper",
                        lambda *a: pytest.fail("no gate may be materialized for a session Service"))

    plan = await sup.build_desired_state()

    assert plan.ready is True and plan.helpers == ()
    assert "not currently available" in plan.caddyfile
    assert "forward_auth" not in plan.caddyfile


# ── auto-hf3ow: Settings reads off the loop, keyed, and coalesced ─────────


@pytest.mark.asyncio
async def test_planner_reads_each_orgs_settings_off_the_event_loop(monkeypatch):
    import threading
    seen = []
    monkeypatch.setattr(sup, "_discover_orgs", lambda: ["autonomy", "anchore"])

    def snapshot(org):
        seen.append((org, threading.current_thread() is threading.main_thread()))
        return None
    monkeypatch.setattr(sup, "_org_publication_snapshot", snapshot)
    await sup._build_desired_state()
    assert seen == [("autonomy", False), ("anchore", False)]


@pytest.mark.asyncio
async def test_a_burst_of_relevant_events_reconciles_once():
    observed = []
    release = asyncio.Event()

    class Supervisor:
        async def reconcile(self, plan, force=False):
            observed.append(plan)
            if len(observed) == 1:
                await release.wait()   # the startup pass is busy
            return {"state": "healthy"}

        def status(self):
            return {"state": "healthy"}

    async def planner():
        return desired()

    class EventBus:
        def __init__(self):
            self.queue = asyncio.Queue()

        def subscribe(self, **_kwargs):
            return self.queue

        def unsubscribe(self, _queue):
            pass

    bus = EventBus()
    worker = sup.GatewayReconcileWorker(
        supervisor=Supervisor(), planner=planner,
        lease_reconciler=NoopLeaseReconciler(),
    )
    await worker.start(bus)
    for _ in range(100):
        if observed:
            break
        await asyncio.sleep(0)
    for seq in range(5):   # arrives while the startup pass runs
        await bus.queue.put(("session:registry", {}, seq))
    release.set()
    for _ in range(200):
        await asyncio.sleep(0)
    await worker.stop()
    assert len(observed) == 2, "startup pass + ONE pass for the burst"


def test_member_by_key_reads_one_key_not_the_whole_set(monkeypatch):
    from tools.dashboard import service_publication as sp
    from tools.graph import settings_ops

    def whole_set(*a, **k):
        raise AssertionError("read the whole set to find one key")
    monkeypatch.setattr(settings_ops, "read_owned_set", whole_set)
    calls = []

    def by_key(set_id, key, *, org, peers=None):
        calls.append((set_id, key, org, peers))
        return {"id": "row-1", "key": key, "payload": {"state": "active"}} \
            if key == "k1" else None
    monkeypatch.setattr(settings_ops, "read_set_key", by_key)
    member = sp._member_by_key("autonomy", "k1")
    assert (member.id, member.key, member.payload) == ("row-1", "k1", {"state": "active"})
    assert sp._target_member_by_key("autonomy", "missing") is None
    assert calls[0] == (sp.NAMESPACE_RESERVATION_SET_ID, "k1", "autonomy", [])
    assert calls[1][0] == sp.SERVICE_TARGET_SET_ID
