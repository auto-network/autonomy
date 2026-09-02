from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from tools.dashboard import web_gateway_supervisor as sup
from tools.dashboard.service_gateway import ServiceGatewayRoute


class FakeRuntime:
    def __init__(self):
        self.running = False
        self.healthy = False
        self.starts = 0
        self.stops = 0
        self.marker = "not-started"

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


def desired(*routes, config="config", ready=True, reason=None):
    return sup.GatewayDesiredState(
        caddyfile=config,
        routes=tuple(sup.DesiredRoute(route_id, fingerprint) for route_id, fingerprint in routes),
        ready=ready,
        reason=reason,
    )


class NoopLeaseReconciler:
    async def reconcile(self):
        pass


@pytest.fixture(autouse=True)
def materialized_service_certificate(monkeypatch):
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
        sup.service_publication,
        "list_service_targets",
        lambda _org: [
            {"reservation_id": "active"},
            {"reservation_id": "paused"},
            {"reservation_id": "released"},
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
        async def reconcile(self, plan):
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
        async def reconcile(self, plan):
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


def test_gateway_org_discovery_excludes_the_personal_store(monkeypatch):
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
        ],
    )

    assert sup._discover_orgs() == ["autonomy"]


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
        desired(("r1", "route-v1"), ready=False, reason="connector-unavailable")
    )

    assert result["state"] == "stopped"
    assert result["reason"] == "connector-unavailable"
    assert result["advertised_routes"] == []
    assert runtime.stops == 1


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
        sup.service_publication,
        "list_service_targets",
        lambda org: [
            {"reservation_id": active_id},
            {"reservation_id": paused_id},
            {"reservation_id": released_id},
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
    assert f"reverse_proxy auto-0831-171125:8000" in plan.caddyfile
    assert f"https://{paused_host}:9443" in plan.caddyfile
    assert 'respond "Service unavailable" 503' in plan.caddyfile
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
        sup.service_publication,
        "list_service_targets",
        lambda _org: [{"reservation_id": "r1"}],
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
        sup.service_publication,
        "list_service_targets",
        lambda org: [{"reservation_id": "r1"}],
    )

    async def connector_not_ready(_org):
        return False

    monkeypatch.setattr(sup, "_connector_ready", connector_not_ready)

    plan = await sup.build_desired_state()

    assert plan.routes == ()
    assert plan.ready is False
    assert plan.reason == "connector-unavailable"


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
        sup.service_publication,
        "list_service_targets",
        lambda org: [{"reservation_id": "r1"}],
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
    assert 'respond "Service unavailable" 503' in plan.caddyfile


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
        async def reconcile(self, plan):
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
        async def reconcile(self, plan):
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
